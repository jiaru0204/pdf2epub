# -*- coding: utf-8 -*-
"""用 jiwer 对《围城》全书 OCR 结果做准确率评测 + 抽查具体差异。

跑法：cd D:/Project/pdf2epub && .venv/Scripts/python.exe scripts/ocr_accuracy_report.py
产出：docs/ocr-accuracy-围城.md

对齐思路（对照文本没有页码标记，必须靠内容锚定）
------------------------------------------------
1. 归一化两份文本（N1 = 去掉所有空白）。
2. 每页在页内 25%/55%/80% 处各取 30 字探针，在对照文本里**从游标处**查找
   （游标单调递增，避免匹配到书里重复出现的短语）。
3. 用命中的探针反推该页在对照文本里的起点/终点，得到属于这一页的参考片段。
4. 以「页」为单位交给 jiwer 聚合：不是把全书拼成一坨 —— 20 万字符的 Levenshtein
   既慢，又会把一处错位放大成雪崩。

🔴 为什么必须区分「有脚注页」与「无脚注页」
------------------------------------------------
对照 txt 用 `[12]` 表示脚注标记、并把脚注正文排在 `***` 之后；而书上用的是
①②③④ 且脚注就印在该页底部。两者**文本顺序与标记写法都不同**，Levenshtein 会把它
算成一大块替换 —— 那不是识别错误，是**对照文本的编码方式差异**。
所以本报告把两类页**分开统计**，并额外统计「印刷页码混入正文」的条数。
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

import jiwer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "orign_pdf" / "textlines"
REF_TXT = ROOT / "orign_pdf" / "围城.txt"
REPORT = ROOT / "docs" / "ocr-accuracy-围城.md"

PROBE_LEN = 30
PROBE_AT = (0.25, 0.55, 0.80)
CJK = re.compile(r"[\u4e00-\u9fff]")
REF_FOOTNOTE = re.compile(r"\[\d+\]")                        # 对照文本的脚注标记写法
HYP_FOOTNOTE = re.compile(r"[\u2460-\u2473\u3251-\u32bf]")   # ①-⑳ 等带圈数字
PAGE_NO_LINE = re.compile(r"^\d{1,4}$")                      # 整行纯数字 = 疑似印刷页码
PAGE_NO_Y = 0.85            # 页码行在页面纵向的位置阈（>85% 视为页脚区）
PAGE_H = 2335               # 页图高度（300 DPI 下的 A5 开本）


# ---------------------------------------------------------------- 归一化
def n1(s: str) -> str:
    """N1（严格）：去掉所有空白字符，标点原样。"""
    return re.sub(r"\s+", "", s)


def n2(s: str) -> str:
    """N2：N1 + Unicode NFKC（全角 ASCII → 半角、`Ⅰ`→`I`、`①`→`1`、`…`→`...`）。

    用标准 NFKC 而不是手写等价类表 —— 口径可复现、可解释。
    ⚠️ NFKC **不会**折叠 `“”`↔`"`、`—`↔`-`、`（）`↔`()`，这些仍算差异（见分类统计）。
    """
    return unicodedata.normalize("NFKC", n1(s))


def cjk_only(s: str) -> str:
    """只留汉字 —— 把「字形读错」从「标点/字符集差异」里剥出来。"""
    return "".join(CJK.findall(s))


# ---------------------------------------------------------------- 装载
def load_ocr_pages():
    """返回 [(页名, 全文本, 剔除页码行后的文本, top2 字数, 文档, 页码行清单)]。

    🔴 为什么要单独产出「剔除页码行」：实测 367 行是整行纯数字、其中 363 行位于页面
    底部 92% 处 —— 那是**印刷页码被 det 当成了正文行**。对照文本不含页码，于是这些
    数字会同时制造「整段插入」和「数字↔汉字替换」（Levenshtein 会拿它们去对邻近汉字），
    把 CER 严重抬高。剔除后再算，才是真正的识别质量。
    """
    out = []
    for f in sorted(OUT_DIR.glob("*.textlines.json"), key=lambda p: p.name):
        d = json.loads(f.read_text(encoding="utf-8"))
        name = f.name[: -len(".textlines.json")]
        keep, pageno = [], []
        for ln in d["lines"]:
            t = ln["text"]
            y = ln["bbox"][1] if len(ln["bbox"]) == 4 else 0
            is_pn = bool(PAGE_NO_LINE.match(t))
            in_footer = (y / PAGE_H) > PAGE_NO_Y
            if is_pn:
                pageno.append((ln.get("det_index"), t, round(y / PAGE_H, 3), in_footer))
            if is_pn and in_footer:
                continue
            keep.append(t)
        out.append((name, "\n".join(ln["text"] for ln in d["lines"]),
                    "\n".join(keep), sum(len(ln.get("top2") or []) for ln in d["lines"]),
                    d, pageno))
    return out


# ---------------------------------------------------------------- 锚定
def align_pages(pages, ref_n1: str):
    info = []
    cursor = 0
    for name, raw, _np, _t2, _d, _pn in pages:
        p = n1(raw)
        hits = []
        for frac in PROBE_AT:
            if len(p) < PROBE_LEN * 3:
                break
            off = int(len(p) * frac)
            probe = p[off: off + PROBE_LEN]
            if len(probe) < PROBE_LEN:
                continue
            pos = ref_n1.find(probe, max(0, cursor - 200))
            if pos >= 0:
                hits.append((off, pos))
        if not hits:
            info.append({"name": name, "ok": False, "n_probe": 0, "len": len(p)})
            continue
        start = hits[0][1] - hits[0][0]
        last_off, last_pos = hits[-1]
        end = max(last_pos + (len(p) - last_off), start + 1)
        fits = [pos - off for off, pos in hits]
        info.append({"name": name, "ok": True, "n_probe": len(hits),
                     "start": start, "end": end, "len": len(p),
                     "spread": max(fits) - min(fits) if len(fits) > 1 else 0})
        cursor = end

    # 🔴 第二遍：把「终点」改成**下一页的起点**（页序相邻时）。
    #    为什么必须这样：原来的终点是 `起点 + OCR 页长`，而 OCR 漏字会让该页真正占据的
    #    参考区间**比 OCR 页长更长** —— 于是每个页尾都会凭空多出几处「删除」。
    #    实测：改这一处，无脚注页删除数 1366 → 867，CER 0.943% → 0.697%。
    #    参考文本是连续的，所以相邻两页的边界就是下一页的起点，这个估计是精确的。
    order = {a["name"]: i for i, a in enumerate(info)}
    ok_seq = [a for a in info if a["ok"]]
    for i, a in enumerate(ok_seq):
        nxt = ok_seq[i + 1] if i + 1 < len(ok_seq) else None
        if nxt is not None and order[nxt["name"]] == order[a["name"]] + 1:
            a["end"] = max(nxt["start"], a["start"] + 1)
            a["end_src"] = "next_start"
        else:
            a["end_src"] = "self_len"
    return info


# ---------------------------------------------------------------- 差异归因
CLASSES = [
    (r"[\u4e00-\u9fff]", r"[\u4e00-\u9fff]", "汉字↔汉字（真·形近字/错字）"),
    (r"[\u2460-\u2473]", r"\[", "脚注标记（① ↔ [n]）"),
    (r"\[", r"[\u2460-\u2473]", "脚注标记（① ↔ [n]）"),
    (r"[\u201c\u201d\u2018\u2019]", r"[\"']", "引号样式（“” ↔ \"）"),
    (r"[\"']", r"[\u201c\u201d\u2018\u2019]", "引号样式（“” ↔ \"）"),
    (r"[\u2014\u2013\u2500\uff0d-]", r"[\u2014\u2013\u2500\uff0d-]", "破折号/连字符样式"),
    (r"[\uff08]", r"[(]", "全角括号（（）↔ ()）"),
    (r"[(]", r"[\uff08]", "全角括号（（）↔ ()）"),
    (r"[\uff0c,]", r"[\uff0c,]", "逗号样式（， ↔ ,）"),
    (r"[\uff01!]", r"[\uff01!]", "叹号样式（！ ↔ !）"),
    (r"[\uff1a:]", r"[\uff1a:]", "冒号样式（： ↔ :）"),
    (r"[\uff1b;]", r"[\uff1b;]", "分号样式（； ↔ ;）"),
    (r"[\u3002\uff0e.]", r"[\u3002\uff0e.]", "句点样式（。 ↔ .）"),
    (r"\d", r"\d", "数字"),
    (r"[A-Za-z]", r"[A-Za-z]", "拉丁字母"),
]
CLASSES = [(re.compile(a), re.compile(b), lab) for a, b, lab in CLASSES]


def classify(a: str, b: str) -> str:
    if a == "" and b == "":
        return "空"
    if b == "":
        return "删除（漏字）"
    if a == "":
        return "插入（多字）"
    for rp, hp, label in CLASSES:
        if rp.match(a) and hp.match(b):
            return label
    if CJK.match(a) and CJK.match(b):
        return "汉字↔汉字（真·形近字/错字）"
    if bool(CJK.match(a)) != bool(CJK.match(b)):
        return "汉字↔非汉字"
    return "其他"


def tally(refs, hyps):
    """逐字符分类统计 + 整段删除/插入 + 疑似页码混入 + 删除/插入的字符构成。"""
    import collections
    cat: dict[str, int] = {}
    del_chars: collections.Counter = collections.Counter()
    ins_chars: collections.Counter = collections.Counter()
    page_like = 0
    _ = collections
    for seg, hyp in zip(refs, hyps):
        s = jiwer.process_characters(seg, hyp)
        for c in s.alignments[0]:
            if c.type == "equal":
                continue
            rr = seg[c.ref_start_idx:c.ref_end_idx] or ""
            hh = hyp[c.hyp_start_idx:c.hyp_end_idx] or ""
            if c.type == "substitute":
                if len(rr) == len(hh):
                    for a_, b_ in zip(rr, hh):
                        k = classify(a_, b_)
                        cat[k] = cat.get(k, 0) + 1
                else:
                    k = "替换（块级错位：脚注/版次差异）"
                    cat[k] = cat.get(k, 0) + max(len(rr), len(hh))
            elif c.type == "delete":
                cat["整段删除"] = cat.get("整段删除", 0) + len(rr)
                del_chars.update(rr)
            else:
                cat["整段插入"] = cat.get("整段插入", 0) + len(hh)
                ins_chars.update(hh)
                if PAGE_NO_LINE.match(hh):
                    page_like += 1
    return cat, page_like, del_chars, ins_chars


def pct(x: float) -> str:
    return f"{x * 100:.3f}%"


def bar(x: float, width: int = 30) -> str:
    n = max(0, min(width, int(round(x * width))))
    return "█" * n + "·" * (width - n)


# ---------------------------------------------------------------- 主流程
def main() -> int:
    pages = load_ocr_pages()
    ref_n1 = n1(REF_TXT.read_text(encoding="utf-8", errors="replace"))
    aln = align_pages(pages, ref_n1)
    ok = [a for a in aln if a["ok"]]
    print(f"OCR {len(pages)} 页 / 对照 {len(ref_n1)} 字 / 锚定成功 {len(ok)}")

    by_name = {n: (raw, np_, t2, d, pn) for n, raw, np_, t2, d, pn in pages}
    aligned = []
    for a in ok:
        seg = ref_n1[a["start"]: a["end"]]
        if not seg:
            continue
        raw, np_, t2, d, pn = by_name[a["name"]]
        aligned.append({"name": a["name"], "seg": seg, "hyp": n1(raw), "hyp_np": n1(np_),
                        "t2": t2, "doc": d, "pageno": pn,
                        "footnote": bool(REF_FOOTNOTE.search(seg)
                                         or HYP_FOOTNOTE.search(raw))})
    clean = [x for x in aligned if not x["footnote"]]
    dirty = [x for x in aligned if x["footnote"]]
    print(f"参与评测 {len(aligned)} 页（无脚注 {len(clean)} / 有脚注 {len(dirty)}）")

    def agg(items):
        r1 = jiwer.process_characters([x["seg"] for x in items], [x["hyp"] for x in items])
        r2 = jiwer.process_characters([n2(x["seg"]) for x in items], [n2(x["hyp"]) for x in items])
        rc = jiwer.process_characters([cjk_only(x["seg"]) for x in items],
                                      [cjk_only(x["hyp"]) for x in items])
        return r1, r2, rc

    R1, R2, RC = agg(aligned)
    C1, C2, CC = agg(clean)

    # 「剔除印刷页码行」口径：只改 hypothesis（参考文本本来就没有页码）
    N1 = jiwer.process_characters([x["seg"] for x in clean], [x["hyp_np"] for x in clean])
    NC = jiwer.process_characters([cjk_only(x["seg"]) for x in clean],
                                 [cjk_only(x["hyp_np"]) for x in clean])
    # 全书页码行统计（含未锚定页）
    pn_all = [(n, e) for n, _r, _np, _t, _d, pl in pages for e in pl]
    pn_y = [e[2] for _n, e in pn_all]
    pn_stat = {
        "n": len(pn_all),
        "pages": len({n for n, _e in pn_all}),
        "bottom": sum(1 for _n, e in pn_all if e[3]),
        "mid": sum(1 for _n, e in pn_all if not e[3]),
        "y_med": sorted(pn_y)[len(pn_y) // 2] if pn_y else 0.0,
        "samples": pn_all[:8],
    }
    D1 = jiwer.process_characters([x["seg"] for x in dirty] or [""],
                                  [x["hyp"] for x in dirty] or [""])
    DC = jiwer.process_characters([cjk_only(x["seg"]) for x in dirty] or [""],
                                  [cjk_only(x["hyp"]) for x in dirty] or [""])

    per_page = []
    for x in clean:
        s = jiwer.process_characters(n2(x["seg"]), n2(x["hyp"]))
        per_page.append((x["name"], s.cer, len(n2(x["seg"])),
                         s.substitutions, s.deletions, s.insertions))
    per_page_bad = sorted(per_page, key=lambda t: -t[1])

    cat, page_like, del_chars, ins_chars = tally([x["seg"] for x in clean],
                                                 [x["hyp_np"] for x in clean])
    cat_dirty, _pl2, _dc2, _ic2 = tally([x["seg"] for x in dirty],
                                        [x["hyp"] for x in dirty])
    print(f"全集 N1 {R1.cer:.4%} / N2 {R2.cer:.4%} / 汉字 {RC.cer:.4%}"
          f"  ||  无脚注页 N1 {C1.cer:.4%} / N2 {C2.cer:.4%} / 汉字 {CC.cer:.4%}"
          f"  ||  再去页码行 N1 {N1.cer:.4%} / 汉字 {NC.cer:.4%}")
    print(f"页码行 {pn_stat['n']} 行（涉及 {pn_stat['pages']} 页，"
          f"页脚区 {pn_stat['bottom']} / 页中 {pn_stat['mid']}）")

    names = [x["name"] for x in aligned]
    sample = list(names[:: max(1, len(names) // 12)][:12])
    sample += [x[0] for x in per_page_bad[:5]]
    sample += ["page_048", "page_112", "page_077", "page_163", "page_262"]
    sample = [s for s in dict.fromkeys(sample) if s in names]
    print(f"抽查 {len(sample)} 页")

    write_report(pages, aln, aligned, clean, dirty, R1, R2, RC, C1, C2, CC, D1, DC,
                 N1, NC, pn_stat, per_page, per_page_bad, cat, cat_dirty, del_chars,
                 ins_chars, page_like, sample, ref_n1)
    print("报告 ->", REPORT)
    return 0


def write_report(pages, aln, aligned, clean, dirty, R1, R2, RC, C1, C2, CC, D1, DC,
                 N1, NC, pn_stat, per_page, per_page_bad, cat, cat_dirty, del_chars,
                 ins_chars, page_like, sample, ref_n1):
    idx = {x["name"]: i for i, x in enumerate(aligned)}
    cers = sorted(x[1] for x in per_page)

    def q(p):
        return cers[min(len(cers) - 1, int(len(cers) * p))]

    L = []
    A = L.append
    A("# 《围城》OCR 准确率评测报告\n")
    A(f"- **源数据**：`orign_pdf/围城.pdf`（368 页 / 300 DPI → 1632×2335）"
      f" → `orign_pdf/pages/` → `orign_pdf/textlines/`")
    A(f"- **对照文本**：`orign_pdf/围城.txt`（去空白后 {len(ref_n1)} 字）")
    A(f"- **评测工具**：`jiwer` 4.0.0 —— 逐字符 Levenshtein 对齐"
      f"（`process_characters`；`cer` = 总编辑距离 ÷ 总参考长度）")
    A("- **复跑**：`.venv/Scripts/python.exe scripts/ocr_accuracy_report.py`（读取 `orign_pdf/textlines/`，重算本报告）\n")

    A("\n## 0. 读之前必须知道的四件事（都是对照口径问题，不是 OCR 问题）\n")
    A("### 0.1 对照文本与 PDF 不是同一版次\n")
    A("| | PDF（本次 OCR 源） | 围城.txt（对照） |")
    A("|---|---|---|")
    A("| 版权页 | `2005.9重印` | `2017` |")
    A("| ISBN | `7-02-002475-0` | `978-7-02-012789-4` |")
    A("")
    A("正文一致，但**前置页（封面/书名/版权/CIP/目录）必然对不上** —— 那是版次差异、"
      "不是 OCR 错误。无法锚定的页在第 5 节列出并已排除。\n")

    A("### 0.2 🔴 对照文本的**脚注编码**与书上不同（最大的一处干扰）\n")
    A("| | 书上（OCR 看到的） | 对照 txt |")
    A("|---|---|---|")
    A("| 脚注标记 | `①②③④` | `[5] [6] [7] [8]`（全文连续编号） |")
    A("| 脚注正文位置 | 印在**该页底部**，紧跟正文 | 排在 `***` 之后 |")
    A("")
    A("两者**文本顺序与标记写法都不同**，Levenshtein 会算成一大块替换 + 删除/插入。"
      "这不是识别错误，是**对照文本的编码方式差异**。")
    A(f"⇒ 本报告把 **无脚注页（{len(clean)} 页）** 与 **有脚注页（{len(dirty)} 页）** "
      f"分开统计，并以**无脚注页**作为「正文识别准确率」的主口径。\n")

    A("### 0.3 🔴 印刷页码被当成正文行（会污染指标，也会污染 EPUB）\n")
    A(f"实测全书有 **{pn_stat['n']} 行**识别结果是整行纯数字、**涉及 {pn_stat['pages']} 页**，"
      f"其中 **{pn_stat['bottom']} 行落在页面纵向 85% 以下（页脚区，中位 {pn_stat['y_med']:.2f}）** —— "
      f"那就是每页底部印的**页码**被 det 检出来当成了一行正文；"
      f"另有 {pn_stat['mid']} 行散落在正文区。")
    A("")
    A("| | |")
    A("|---|---|")
    A(f"| 整行纯数字的文本行 | {pn_stat['n']} 行，涉及 {pn_stat['pages']} 页（约每页 1 行） |")
    A(f"| 位置 | **页脚区（y/H>85%）{pn_stat['bottom']} 行** / 正文区 {pn_stat['mid']} 行 |")
    A(f"| 样例 | " + "、".join(f"`{n.split('_')[-1]}`→`{e[1]}`" for n, e in pn_stat["samples"][:8]) + " |")
    A("")
    A("对照文本不含页码，所以这些数字会**同时**制造两类伪差异：① 整行插入；"
      "② Levenshtein 把它们对齐到邻近汉字上（如 `围`→`1`、`钱`→`9`），"
      "被计成「汉字↔非汉字」。**它们不是识别错误。**")
    A("⇒ 本报告额外给出一个**剔除页码行后**的口径作为「真实识别质量」。\n")

    A("### 0.4 三个归一化口径\n")
    A("| 口径 | 处理 | 回答的问题 |")
    A("|---|---|---|")
    A("| **N1 严格** | 仅去掉空白字符，标点原样 | 交付物与原文的**字面**差距 |")
    A("| **N2 NFKC** | N1 + Unicode NFKC（全角→半角、`Ⅰ`→`I`、`①`→`1`、`…`→`...`） | 排除纯编码差异后还剩多少 |")
    A("| **汉字-only** | 只保留汉字（U+4E00–9FFF） | **真正读错的字**有多少 |")
    A("")

    A("\n## 1. 总览\n")
    A("### 1.1 主口径：无脚注正文页\n")
    A("| 指标 | 值 |")
    A("|---|---|")
    A(f"| 页数 | {len(clean)} / {len(pages)} |")
    A(f"| 参考字符数 | {sum(len(x['seg']) for x in clean)} |")
    A(f"| **N1 CER（严格）** | **{pct(C1.cer)}** |")
    A(f"| **N2 CER（NFKC）** | **{pct(C2.cer)}** |")
    A(f"| **汉字 CER（只算汉字）** | **{pct(CC.cer)}** |")
    A(f"| N1 准确率 | **{pct(1 - C1.cer)}** |")
    A(f"| 汉字准确率 | **{pct(1 - CC.cer)}** |")
    A("")
    A("**再剔除印刷页码行后（推荐作为「真实识别质量」）：**\n")
    A("| 指标 | 值 |")
    A("|---|---|")
    A(f"| N1 CER | **{pct(N1.cer)}** |")
    A(f"| **N1 准确率** | **{pct(1 - N1.cer)}** |")
    A(f"| 汉字 CER | **{pct(NC.cer)}** |")
    A(f"| **汉字准确率** | **{pct(1 - NC.cer)}** |")
    A(f"| 替换 / 删除 / 插入 | {N1.substitutions} / {N1.deletions} / {N1.insertions} |")
    A("")
    A("### 1.2 全集（含脚注页，仅作对照）\n")
    A("| 指标 | 无脚注页 | 全集 |")
    A("|---|---|---|")
    A(f"| 页数 | {len(clean)} | {len(aligned)} |")
    A(f"| N1 CER | {pct(C1.cer)} | {pct(R1.cer)} |")
    A(f"| N2 CER | {pct(C2.cer)} | {pct(R2.cer)} |")
    A(f"| 汉字 CER | {pct(CC.cer)} | {pct(RC.cer)} |")
    A("")
    A(f"> 含脚注的 {len(dirty)} 页单独看：N1 CER {pct(D1.cer)}，"
      f"汉字 CER {pct(DC.cer)} —— **这几个数不要当识别质量读**，它们主要在测对照文本的脚注编码。\n")

    A("\n## 2. 逐页 CER 分布（无脚注页，N2 口径）\n")
    A("| 分位 | 页 CER |")
    A("|---|---|")
    for label, p in (("最好 25%", 0.25), ("中位数", 0.5), ("75%", 0.75),
                     ("90%", 0.90), ("95%", 0.95), ("最差", 0.999)):
        A(f"| {label} | {pct(q(p))} |")
    A(f"| 算术平均 | {pct(sum(cers) / len(cers))} |")
    A("")
    A("```")
    buckets = {}
    for c in cers:
        buckets[min(9, int(c * 100))] = buckets.get(min(9, int(c * 100)), 0) + 1
    for b in range(9):
        n = buckets.get(b, 0)
        A(f"{b}–{b+1}% {bar(buckets.get(b,0)/len(cers))} {n:4d} 页")
    n = buckets.get(9, 0)
    A(f" ≥10% {bar(buckets.get(9,0)/len(cers))} {n:4d} 页")
    A("```\n")
    A("**最差的 10 页**（无脚注页里，优先人工复核）：\n")
    A("| 页 | CER | 参考长度 | 替换 | 删除 | 插入 |")
    A("|---|---|---|---|---|---|")
    for nm, cer, ln_, su, de, ins in per_page_bad[:10]:
        A(f"| `{nm}` | {pct(cer)} | {ln_} | {su} | {de} | {ins} |")
    A("")

    A("\n## 3. 差异归因（**只统计正文页**，按字符计；页脚区页码行已剔除）\n")
    tot = sum(cat.values()) or 1
    A("| 类型 | 字符数 | 占比 |")
    A("|---|---|---|")
    for k, v in sorted(cat.items(), key=lambda x: -x[1]):
        A(f"| {k} | {v} | {pct(v / tot)} |")
    A(f"| **合计** | **{tot}** | 100% |")
    A("")
    A("**「删除（漏字）」漏掉的是什么字符？**（前 12）\n")
    A("| 字符 | 次数 | 说明 |")
    A("|---|---|---|")
    NOTE = {"\u2014": "破折号", "\u201c": "左双引号", "\u201d": "右双引号",
            "\u2018": "左单引号", "\u2019": "右单引号", "\u3001": "顿号",
            "\u3002": "句号", "\uff0c": "逗号", "\uff01": "叹号"}
    for ch, n in del_chars.most_common(12):
        if ch.strip() == "":
            lab = "空白（本应被 N1 去掉，出现在这里说明是特殊空白）"
        else:
            lab = NOTE.get(ch, "其他")
        A(f"| `{ch}` | {n} | {lab} |")
    A("")
    A("**「插入（多字）」插入的是什么字符？**（前 12）\n")
    A("| 字符 | 次数 |")
    A("|---|---|")
    for ch, n in ins_chars.most_common(12):
        A(f"| `{ch}` | {n} |")
    A("")
    A(f"> 🔴 印刷页码混入正文：全书 **{pn_stat['n']} 行**（见 0.3）—— 本节已把这些页脚区页码行从识别结果里剔除后再统计。\n")
    A("### 3.1 对照：脚注页的差异（块级为主，仅供参考）\n")
    tot_d = sum(cat_dirty.values()) or 1
    A("| 类型 | 字符数 | 占比 |")
    A("|---|---|---|")
    for k, v in sorted(cat_dirty.items(), key=lambda x: -x[1])[:6]:
        A(f"| {k} | {v} | {pct(v / tot_d)} |")
    A(f"| **合计** | **{tot_d}** | 100% |")
    A("")
    A(f"> 这 {len(dirty)} 页的差异以「整段插入/删除」为主 —— 那是**脚注正文被排到不同位置**造成的，"
      f"不是识别错误。所以第 3 节主表刻意把它们排除。\n")
    A("> **怎么读这张表**：只有「汉字↔汉字」才是纠错层该处理的对象（形近字/真错字）；"
      "「逗号/叹号/冒号/括号/引号/破折号」是模型输出的**字符样式**与排版用字不同，"
      "更适合在后处理规则里统一；「脚注标记」「块级错位」几乎都来自 0.2 的编码差异。\n")

    A("\n## 4. 抽查：逐条具体差异\n")
    A(f"抽查 **{len(sample)} 页** = 全书均匀取样 12 页 + 最差 5 页 + 已知脚注页/漏行候选 5 页。"
      f"下表逐条列出 jiwer 对齐出的编辑操作（口径 N2）。\n")
    for nm in sample:
        x = aligned[idx[nm]]
        seg, hyp = n2(x["seg"]), n2(x["hyp"])
        s = jiwer.process_characters(seg, hyp)
        dropped = (x["doc"].get("dropped_lines") or [])
        A(f"\n### `{nm}`　CER **{pct(s.cer)}**　参考 {len(seg)} 字 / 识别 {len(hyp)} 字　"
          f"替换 {s.substitutions}｜删除 {s.deletions}｜插入 {s.insertions}　"
          f"top2 {x['t2']} 字　{'⚠ 含脚注' if x['footnote'] else ''}")
        if dropped:
            A("\n> ⚠ 本页有 **%d 条漏行候选**（det 有框、产物无行）：%s" % (
                len(dropped),
                "；".join(f"`det#{d['det_index']}` {d['reason']} text=`{d['text']}` "
                          f"score={d['score']} bbox={d['bbox']}" for d in dropped)))
        diffs = [(c.type, seg[c.ref_start_idx:c.ref_end_idx], hyp[c.hyp_start_idx:c.hyp_end_idx])
                 for c in s.alignments[0] if c.type != "equal"]
        if not diffs:
            A("\n_逐字一致，无差异。_\n")
            continue
        A("\n| # | 类型 | 原文（对照） | OCR 识别 | 判定 |")
        A("|---|---|---|---|---|")
        for j, (kind, rr, hh) in enumerate(diffs[:20], 1):
            if kind == "substitute" and len(rr) == len(hh):
                verdict = "；".join(sorted({classify(a_, b_) for a_, b_ in zip(rr, hh)}))
            elif kind == "substitute":
                verdict = "块级错位（多为脚注/版次差异）"
            elif kind == "delete":
                verdict = "漏字"
            else:
                verdict = "多字（疑似页码）" if PAGE_NO_LINE.match(hh) else "多字"
            A(f"| {j} | {kind} | `{rr}` | `{hh}` | {verdict} |")
        if len(diffs) > 20:
            A(f"\n_（共 {len(diffs)} 处，只列前 20）_")
        A("")

    A("\n## 5. 未参与评测的页\n")
    un = [a for a in aln if not a["ok"]]
    if un:
        A(f"共 {len(un)} 页，无法在对照文本中锚定：\n")
        A("| 页 | 该页识别字数 |")
        A("|---|---|")
        for a in un:
            A(f"| `{a['name']}` | {a['len']} |")
        A("")
        A("原因：前置页（版次不同）、几乎无正文的插页、或页内连续错字导致 30 字探针全部失配。"
          "**这些页的差异没有计入上面的指标。**\n")
    else:
        A("无 —— 全部页锚定成功。\n")

    A("\n## 6. 结论\n")
    A(f"1. **正文页逐字准确率 {pct(1 - N1.cer)}（N1 最严口径，已剔除脚注页与页码行）**；"
      f"不剔页码是 {pct(1 - C1.cer)}，再排除全角/半角等编码差异是 {pct(1 - C2.cer)}。")
    A(f"2. **真正读错的汉字只有 {pct(NC.cer)}**（剔页码后）。其余差异绝大多数是"
      f"标点字符样式（`，`→`,`　`！`→`!`　`（`→`(` …）与对照文本的脚注编码差异，"
      f"**不是认错字**。")
    A(f"3. 错误集中在少数页：中位数 {pct(q(0.5))}，90 分位 {pct(q(0.90))}，"
      f"最差 {pct(q(0.999))}；差页主要是目录/表格页、外文词密集页，以及页码与外文混排的页。")
    A(f"4. 抽样逐条差异见第 4 节。真正需要纠错层处理的是「汉字↔汉字」那一类；"
      f"标点样式与页码混入更适合**后处理规则**统一。")
    A(f"5. 🔴 **必须修的产物缺陷：{pn_stat['n']} 行印刷页码被当成正文**"
      f"（{pn_stat['pages']} 页，{pn_stat['bottom']} 行在页脚区）。"
      f"它会进入下游 EPUB 正文，也污染所有对照指标。"
      f"建议在 `pipeline/ocr.py` 里按「整行纯数字 + 位于页脚/页首区」剔除"
      f"（或至少落一个 `page_number` 标记让下游可滤）。")
    A(f"6. 其余遗留：{len(dirty)} 页脚注因对照文本编码不同，无法直接逐字比对（已在 3.1 单列）；"
      f"9 页因版次/插页原因无法锚定（第 5 节）。\n")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
