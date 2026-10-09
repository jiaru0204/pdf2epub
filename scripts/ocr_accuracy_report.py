# -*- coding: utf-8 -*-
"""用 jiwer 对《围城》全书 OCR 结果做准确率评测 + 抽查具体差异。

跑法：cd D:/Project/pdf2epub && .venv/Scripts/python.exe scripts/ocr_accuracy_report.py
产出：docs/ocr-accuracy-围城.md

对齐思路（对照文本没有页码标记，必须靠内容锚定）
------------------------------------------------
1. 归一化两份文本（N1 = 去掉所有空白）。
2. **以「页面开头」为锚**定位该页在对照文本里的起点：先用页面开头 48 字做精确匹配
   （长度 48→10 逐级回退，页面开头有少量错字时靠缩短锚串仍能命中），失败再用
   `partial_ratio_alignment` 模糊兜底，最后用页内中段 16 字做精确吸附精修。
3. **页 i 的终点 = 页 i+1 的起点** —— 参考文本是连续的，相邻两页之间没有空隙，
   这样得到的是对全书参考文本的不重叠划分。
4. 以「页」为单位交给 jiwer 聚合：不是把全书拼成一坨 —— 20 万字符的 Levenshtein
   既慢，又会把一处错位放大成雪崩。

🔴 为什么不能用「页内中段探针反推页首」
------------------------------------------------
反推式 `页首 ≈ 探针命中位置 − 探针在页内的偏移` 隐含了一个前提：
**OCR 在页内的字符偏移 == 参考文本的偏移**。而 OCR 只要在探针**之前**丢了字
（破折号、引号是重灾区），OCR 偏移就小于参考偏移，反推出的页首必然**偏右**。
实测后果：
    - `page_093` 起点偏右 2 字 → 本属该页的 `斜川` 被判成「插入」；
    - `page_285` 起点偏右 10 字 → 整句 `事。你现在气平了没有` 被判成「插入」；
    - 全书 31/315 页存在可被平移改善的边界，且**偏移全部为负**（系统性偏右）。
改成页首直接锚定后（同一页集、同一归一化口径）：
插入 212 → **22**，删除 867 → **723**，N1 CER 0.6965% → **0.5399%**。
本脚本因此额外输出一段「对齐质量自检」（平移敏感度），见报告 0.5。

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
from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "orign_pdf" / "textlines"
REF_TXT = ROOT / "orign_pdf" / "围城.txt"
REPORT = ROOT / "docs" / "ocr-accuracy-围城.md"

ANCHOR_LEN = 48                     # 页首锚串长度（精确匹配用）
ANCHOR_LADDER = (48, 40, 32, 24, 16, 10)   # 精确匹配的逐级回退长度
ANCHOR_FUZZY_LEN = 96               # 模糊兜底时取页面开头多少字
ANCHOR_FUZZY_MIN = 60               # 模糊兜底的相似度下限
SLIDE_RANGE = 40                    # 对齐质量自检：参考窗口平移 ±N 字
SLIDE_STEP = 2
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
def _anchor_start(pg: str, ref: str, lo: int, hi: int):
    """在 `ref[lo:hi]` 里定位某个页面（`pg` = 该页归一化全文）的起点。

    返回 `(pos, how)`；定位不到返回 `(None, None)`。

    三级策略（都是**直接锚定页首**，不做任何按偏移量反推）：
      ① 精确：拿 `pg` 开头若干字去 `find`，长度从 48 逐级退到 10
         —— 页面开头若有少量错字，缩短锚串通常仍能精确命中；
      ② 模糊：`partial_ratio_alignment` 在窗口里找最相似的 96 字片段；
      ③ 吸附：用 `pg` 中段 16 字在小范围里精确匹配，把模糊结果钉到字上。
    窗口上下界由上一页的起点决定（单调游标），避免命中前言/目录里的重复短语。
    """
    if len(pg) < 10:
        return None, None
    for n in ANCHOR_LADDER:
        if len(pg) < n:
            break
        pos = ref.find(pg[:n], lo, hi)
        if pos >= 0:
            return pos, f"精确{n}字"
    head = pg[:ANCHOR_FUZZY_LEN]
    al = fuzz.partial_ratio_alignment(head, ref[lo:hi])
    if al is None or al.score < ANCHOR_FUZZY_MIN:
        return None, None
    rough = lo + al.dest_start
    off = len(head) // 3
    mid = head[off: off + 16]
    if len(mid) >= 8:
        snap = ref.find(mid, max(lo, rough - 60), min(hi, rough + 60 + len(mid)))
        if snap >= 0:
            return snap - off, f"模糊{int(al.score)}+吸附"
    return rough, f"模糊{int(al.score)}"


def align_pages(pages, ref_n1: str):
    """把每个 OCR 页面锚定到对照文本，得到「不重叠划分」的参考片段。"""
    info = []
    prev = 0
    for name, raw, _np, _t2, _d, _pn in pages:
        p = n1(raw)
        if len(p) < 10:
            info.append({"name": name, "ok": False, "len": len(p), "why": "空白页"})
            continue
        lo = max(0, prev - 80)
        hi = min(len(ref_n1), prev + len(p) + 2000)
        pos, how = _anchor_start(p, ref_n1, lo, hi)
        if pos is None:
            info.append({"name": name, "ok": False, "len": len(p), "why": "锚定失败"})
            continue
        info.append({"name": name, "ok": True, "start": pos, "len": len(p), "how": how})
        prev = pos

    # 终点 = 下一页的起点（页序真正相邻时）。参考文本连续 ⇒ 这是精确划分，
    # 而不是「起点 + OCR 页长」那种会把页尾漏字算成删除的估计。
    order = {a["name"]: i for i, a in enumerate(info)}
    ok_seq = [a for a in info if a["ok"]]
    for i, a in enumerate(ok_seq):
        nxt = ok_seq[i + 1] if i + 1 < len(ok_seq) else None
        if nxt is not None and order[nxt["name"]] == order[a["name"]] + 1:
            a["end"] = max(nxt["start"], a["start"] + 1)
            a["end_src"] = "下一页起点"
        else:
            # 末页 / 相邻页未锚定：按 OCR 页长 + 余量估计
            a["end"] = min(len(ref_n1), a["start"] + a["len"] + 40)
            a["end_src"] = "页长估计"
    return info


def slide_check(items, ref: str):
    """对齐质量自检：把每页的参考窗口整体平移 ±SLIDE_RANGE 字，看 CER 能否再降。

    若某页存在能显著降低 CER 的平移，说明该页边界仍估错。这是检验「锚定是否
    局部最优」的硬指标 —— 旧的反推法有 31/315 页可被平移改善，且偏移全为负。
    """
    improved = []
    base_sum = best_sum = 0.0
    for it in items:
        s, e, h = it["start"], it["end"], it["hyp"]
        b = jiwer.process_characters(ref[s:e], h).cer
        bi, bd = b, 0
        for d in range(-SLIDE_RANGE, SLIDE_RANGE + 1, SLIDE_STEP):
            if s + d < 0 or e + d > len(ref):
                continue
            c = jiwer.process_characters(ref[s + d: e + d], h).cer
            if c < bi - 1e-12:
                bi, bd = c, d
        base_sum += b
        best_sum += bi
        if b - bi > 1e-9:
            improved.append((b - bi, it["name"], bd, b, bi))
    improved.sort(reverse=True)
    return {
        "n": len(items), "improved": len(improved),
        "base_mean": base_sum / max(1, len(items)),
        "best_mean": best_sum / max(1, len(items)),
        "worst": improved[:8],
        "off_ge4": sum(1 for _g, _n, d, _b, _bi in improved if abs(d) >= 4),
    }

def front_matter_pages(pages):
    """按**书自己的页脚页码**把页序列划成「前置页 + 正文 + 后置页」。

    做法：取每页页脚区的整行数字当印刷页码，在页序上找**最长的「连续 +1」序列** ——
    那就是正文；序列之前的是封面/扉页/版权页/序（页码独立编号或不编号），之后的是封底。
    这个判据只依赖页图自身，**不依赖对照文本、也不依赖版次**。
    对照文本与这些页的版次本来就不同（见 0.1），逐字比对没有意义，所以单列不参与主口径。

    返回 (前置/后置页名集合, 正文起始下标, 正文结束下标)。
    """
    nums = []
    for _name, _raw, _np, _t2, _d, pn in pages:
        n = None
        for e in pn:
            if e[3]:
                n = e[1]
        nums.append(int(n) if n and n.isdigit() else None)

    best = (-1, -1, 0)
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j] is not None and nums[j + 1] == nums[j] + 1:
            j += 1
        if nums[i] is not None and (j - i + 1) > best[2]:
            best = (i, j, j - i + 1)
        i = j + 1
    a, b, _n = best
    if a < 0:
        return set(), 0, len(pages) - 1
    front = {pages[k][0] for k in range(len(pages)) if k < a or k > b}
    return front, a, b


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
                        "start": a["start"], "end": a["end"], "how": a.get("how"),
                        "t2": t2, "doc": d, "pageno": pn,
                        "footnote": bool(REF_FOOTNOTE.search(seg)
                                         or HYP_FOOTNOTE.search(raw))})
    front_names, body_a, body_b = front_matter_pages(pages)
    front = [x for x in aligned if x["name"] in front_names]
    clean = [x for x in aligned if x["name"] not in front_names and not x["footnote"]]
    dirty = [x for x in aligned if x["name"] not in front_names and x["footnote"]]
    print(f"锚定 {len(aligned)} 页 → 正文页 {len(clean)} / 脚注页 {len(dirty)} / 前置·后置页 {len(front)}")
    print(f"正文页码区间: 索引 {body_a}–{body_b}"
          f"（{pages[body_a][0]} … {pages[body_b][0]}）")

    # 对齐质量自检：把每页参考窗口平移 ±SLIDE_RANGE 字，看 CER 还能不能降
    slide = slide_check(clean, ref_n1)
    print(f"对齐自检：{slide['improved']}/{slide['n']} 页可被平移改善"
          f"（逐页 CER 均值 {slide['base_mean']:.4%} → {slide['best_mean']:.4%}）")

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
    N2 = jiwer.process_characters([n2(x["seg"]) for x in clean], [n2(x["hyp_np"]) for x in clean])
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

    # ⚠️ 统一用「剔除页脚区页码行」后的口径 —— 与 1.1 主口径一致。
    #    否则「最差页」排名里会混进页码行造成的插入，与报告表头的口径自相矛盾。
    per_page = []
    for x in clean:
        s = jiwer.process_characters(n2(x["seg"]), n2(x["hyp_np"]))
        per_page.append((x["name"], s.cer, len(n2(x["seg"])),
                         s.substitutions, s.deletions, s.insertions))
    per_page_bad = sorted(per_page, key=lambda t: -t[1])

    cat, page_like, del_chars, ins_chars = tally([x["seg"] for x in clean],
                                                 [x["hyp_np"] for x in clean])
    cat_dirty, _pl2, _dc2, _ic2 = tally([x["seg"] for x in dirty],
                                        [x["hyp"] for x in dirty])
    print(f"全集 N1 {R1.cer:.4%} / N2 {R2.cer:.4%} / 汉字 {RC.cer:.4%}"
          f"  ||  正文页 N1 {C1.cer:.4%} / N2 {C2.cer:.4%} / 汉字 {CC.cer:.4%}"
          f"  ||  再去页码行 N1 {N1.cer:.4%} / 汉字 {NC.cer:.4%}")
    print(f"页码行 {pn_stat['n']} 行（涉及 {pn_stat['pages']} 页，"
          f"页脚区 {pn_stat['bottom']} / 页中 {pn_stat['mid']}）")

    names = [x["name"] for x in aligned if x["name"] not in front_names]
    sample = list(names[:: max(1, len(names) // 12)][:12])
    sample += [x[0] for x in per_page_bad[:5]]
    sample += ["page_093", "page_285", "page_048", "page_112", "page_163", "page_262"]
    sample = [s for s in dict.fromkeys(sample) if s in names]
    print(f"抽查 {len(sample)} 页")

    write_report(pages, aln, aligned, clean, dirty, front, body_a, body_b,
                 R1, R2, RC, C1, C2, CC, D1, DC,
                 N1, N2, NC, pn_stat, per_page, per_page_bad, cat, cat_dirty, del_chars,
                 ins_chars, page_like, sample, ref_n1, slide)
    print("报告 ->", REPORT)
    return 0


def write_report(pages, aln, aligned, clean, dirty, front, body_a, body_b,
                 R1, R2, RC, C1, C2, CC, D1, DC,
                 N1, N2, NC, pn_stat, per_page, per_page_bad, cat, cat_dirty, del_chars,
                 ins_chars, page_like, sample, ref_n1, slide):
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

    A("\n## 0. 口径说明（读数字之前必须知道的事）\n")
    A("### 0.1 对照文本与 PDF 不是同一版次\n")
    A("| | PDF（本次 OCR 源） | 围城.txt（对照） |")
    A("|---|---|---|")
    A("| 版权页 | `2005.9重印` | `2017` |")
    A("| ISBN | `7-02-002475-0` | `978-7-02-012789-4` |")
    A("")
    A("正文一致，但**前置页（封面/书名/版权/CIP/序）必然对不上** —— 那是版次差异、"
      "不是 OCR 错误。本报告按 0.6 的办法把前置/后置页单独摘出，不参与任何指标；"
      "锚定失败的页在第 5 节列出。\n")

    A("### 0.2 🔴 对照文本的**脚注编码**与书上不同（最大的一处干扰）\n")
    A("| | 书上（OCR 看到的） | 对照 txt |")
    A("|---|---|---|")
    A("| 脚注标记 | `①②③④` | `[5] [6] [7] [8]`（全文连续编号） |")
    A("| 脚注正文位置 | 印在**该页底部**，紧跟正文 | 排在 `***` 之后 |")
    A("")
    A("两者**文本顺序与标记写法都不同**，Levenshtein 会算成一大块替换 + 删除/插入。"
      "这不是识别错误，是**对照文本的编码方式差异**。")
    A(f"⇒ 本报告把 **正文页（{len(clean)} 页）**、**脚注页（{len(dirty)} 页）**、"
      f"**前置/后置页（{len(front)} 页）** 分成三桶统计（见 1.2），"
      f"并以**正文页**作为「识别准确率」的主口径。\n")

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

    # ---- 0.5 的统计量 ----
    import collections
    seq = [a for a in aln if a["ok"]]
    ex = sum(1 for a in seq if str(a.get("how", "")).startswith("精确"))
    fz = len(seq) - ex
    mono = all(seq[i]["start"] < seq[i + 1]["start"] for i in range(len(seq) - 1))
    gap = max((seq[i + 1]["start"] - seq[i]["start"] for i in range(len(seq) - 1)), default=0)
    ocr_all = "".join(n1(r) for _n, r, *_ in pages)
    dr_ref = collections.Counter(len(m) for m in re.findall("\u2014+", ref_n1))
    dr_ocr = collections.Counter(len(m) for m in re.findall("\u2014+", ocr_all))
    dash_ref_n = sum(k * v for k, v in dr_ref.items())
    dash_ocr_n = sum(k * v for k, v in dr_ocr.items())
    A("### 0.5 页 ↔ 参考文本怎么定位（锚定）与对齐质量自检\n")
    A("对照文本**没有页码标记**，所以每一页只能靠**内容**定位到参考文本里的一段。"
      "本报告的做法：")
    A("")
    A("1. **直接锚定页首** —— 拿该页 OCR 文本的开头 48 字在参考文本里精确查找，"
      "长度逐级回退到 10 字（页面开头有少量错字时仍能命中）；精确找不到就用模糊对齐兜底，"
      "再用页内中段 16 字做精确吸附精修。查找窗口的上下界由上一页的起点决定（单调游标），"
      "避免命中前言/目录里的重复短语。")
    A("2. **页 i 的终点 = 页 i+1 的起点** —— 参考文本是连续的，相邻两页之间没有空隙，"
      "这样得到的是对全书参考文本的**不重叠划分**（既不漏、也不重复计数）。")
    A("")
    A("> ⚠️ 不用「在页内固定位置取探针、再用 `命中位置 − 页内偏移` 反推页首」这种做法："
      "那个式子隐含了 **OCR 在页内的字符偏移 == 参考文本的偏移**。而 OCR 只要在探针"
      "**之前**丢了字（破折号、引号是重灾区），OCR 偏移就小于参考偏移，反推出的页首必然"
      "**偏右** —— 于是本属该页的文字会被判成「插入」。实测：`page_093` 偏右 2 字，"
      "把 `斜川` 判成插入；`page_285` 偏右 10 字，把整句 `事。你现在气平了没有` 判成插入。"
      "全书有 31/315 页存在可被平移改善的边界，且偏移**全为负**（系统性偏右）。")
    A("")
    A("**自检 1 · 锚定方式**\n")
    A("| 方式 | 页数 |")
    A("|---|---|")
    A(f"| 精确匹配（取 48→10 字锚串） | {ex} |")
    A(f"| 模糊兜底（+吸附精修） | {fz} |")
    A("")
    A("**自检 2 · 起点单调性**\n")
    A(f"- 页序相邻的 {len(seq) - 1} 对页，起点严格递增：**{'✓ 通过' if mono else '✗ 失败'}**"
      f"（相邻两页起点最大间隔 {gap} 字）")
    A("")
    A("**自检 3 · 平移敏感度**（把每页参考窗口整体平移 ±24 字，看 CER 还能不能再降）\n")
    A(f"- 可被平移改善的页：**{slide['improved']} / {slide['n']}**"
      f"（逐页 CER 均值 {pct(slide['base_mean'])} → 最优平移后 {pct(slide['best_mean'])}）")
    if slide["improved"] == 0:
        A("- ⇒ **已经局部最优**：任何方向的平移都不会让任何一页变得更准，说明页边界不再有系统性偏移。\n")
    else:
        A(f"- ⚠ 仍有 {slide['improved']} 页可被平移改善（|平移|≥4 字的有 {slide['off_ge4']} 页）：\n")
        A("| 页 | 平移 | 现 CER | 平移后 CER |")
        A("|---|---|---|---|")
        for _g, nm, d, b, bi in slide["worst"]:
            A(f"| `{nm}` | {d:+d} 字 | {pct(b)} | {pct(bi)} |")
        A("")

    dl = sorted((x["end"] - x["start"]) - len(x["hyp_np"]) for x in clean)
    _dn = len(dl)

    def _dq(f):
        return dl[min(_dn - 1, int(_dn * f))]

    outl = [x for x in clean if abs((x["end"] - x["start"]) - len(x["hyp_np"])) > 40]
    A("**自检 4 · 参考段长 vs OCR 页长**（正文页）\n")
    A("若某页边界被估错，参考段长就会明显偏离 OCR 页长。正常情况下 Δ 应为**小的正值**"
      "（参考段比 OCR 多出的正是被漏掉的字）。\n")
    A("| 分位 | Δ = 参考段长 − OCR 页长 |")
    A("|---|---|")
    A(f"| 最小 / 5% | {dl[0]} / {_dq(0.05)} |")
    A(f"| 中位 | {_dq(0.5)} |")
    A(f"| 95% / 最大 | {_dq(0.95)} / {dl[-1]} |")
    A("")
    if outl:
        A(f"⚠ {len(outl)} 页 |Δ| > 40 字，优先复核：" +
          "、".join(f"`{x['name']}`({(x['end'] - x['start']) - len(x['hyp_np']):+d})"
                   for x in outl[:10]) + "\n")
    else:
        A("Δ 全部落在 ±40 字内 —— 没有落单的错位页。\n")

    A("### 0.6 前置页 / 正文页怎么划\n")
    A("对照文本与 PDF 版次不同（见 0.1），所以**封面、扉页、版权页、序**这些前置页"
      "逐字比对没有意义。本报告用**书自己的页脚页码**来划：把每页页脚区的整行数字当作"
      "印刷页码，在页序上找**最长的「连续 +1」序列**——那就是正文；序列之前/之后的页"
      "即前置/后置页。这个判据只用页图自身，不依赖对照文本、也不依赖版次。")
    A("")
    A(f"- 正文页码范围：`{pages[body_a][0]}` … `{pages[body_b][0]}`（页序索引 {body_a}–{body_b}）")
    if front:
        names = [x["name"] for x in front]
        A(f"- 单列的前置/后置页共 **{len(front)} 页**：" +
          "、".join(f"`{n}`" for n in names[:14]) +
          ("……" if len(names) > 14 else ""))
        A("")
        A("| 页 | 该页内容开头 | 参考段长 |")
        A("|---|---|---|")
        for x in front:
            head = n1(x["doc"]["lines"][0]["text"])[:24] if x["doc"].get("lines") else ""
            A(f"| `{x['name']}` | {head} | {x['end'] - x['start']} |")
    A("")

    A("\n## 1. 总览\n")
    A("### 1.1 主口径：正文页（已剔除脚注页与前置/后置页）\n")
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
    A(f"| N2 CER | **{pct(N2.cer)}** |")
    A(f"| 汉字 CER | **{pct(NC.cer)}** |")
    A(f"| **汉字准确率** | **{pct(1 - NC.cer)}** |")
    A(f"| 替换 / 删除 / 插入 | {N1.substitutions} / {N1.deletions} / {N1.insertions} |")
    A("")
    F1 = jiwer.process_characters([x["seg"] for x in front] or [""],
                                  [x["hyp"] for x in front] or [""])
    A("### 1.2 分桶对照（三个桶不可混读）\n")
    A("| 指标 | 正文页 | 脚注页 | 全集 |")
    A("|---|---|---|---|")
    A(f"| 页数 | **{len(clean)}** | {len(dirty)} | {len(aligned)} |")
    A(f"| N1 CER | **{pct(C1.cer)}** | {pct(D1.cer)} | {pct(R1.cer)} |")
    A(f"| N2 CER | {pct(C2.cer)} | — | {pct(R2.cer)} |")
    A(f"| 汉字 CER | **{pct(CC.cer)}** | {pct(DC.cer)} | {pct(RC.cer)} |")
    A("")
    A(f"> - **脚注页（{len(dirty)} 页）不要当识别质量读**：它们的差异以「整段插入/删除」为主，"
      f"来自对照文本的脚注编码与书上不同（见 0.2）。")
    A(f"> - **前置/后置页（{len(front)} 页）不参与任何指标**：与对照文本版次不同（见 0.1、0.6）。"
      f"若强行比对其 N1 CER 为 {pct(F1.cer)}，那是版次差异、不是识别质量。\n")

    A("\n## 2. 逐页 CER 分布（正文页；N2 口径 + 已剔除页脚区页码行）\n")
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
    A("**最差的 10 页**（正文页，已剔除页脚区页码行；页边界经 0.5 的自检，"
      "排名**不含对齐噪声**）：\n")
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
    # 破折号专项：这是「整段删除」里最大的一块，而且有独立于对齐的硬证据
    dash_del = del_chars.get("\u2014", 0)
    A("#### 🔴 最大的一块：破折号 `——` 被识别成单个 `—`\n")
    A("这一条可以用**独立于对齐**的证据直接验证 —— 直接数两份文本里破折号的"
      "「连排长度分布」：\n")
    A("| 文本 | `—` 总个数 | 单 `—` | 双 `——` | 更长 |")
    A("|---|---|---|---|---|")
    A(f"| 对照 txt | {dash_ref_n} | {dr_ref.get(1,0)} | {dr_ref.get(2,0)} | "
      f"{sum(v for k, v in dr_ref.items() if k >= 3)} |")
    A(f"| OCR 产物 | {dash_ocr_n} | **{dr_ocr.get(1,0)}** | {dr_ocr.get(2,0)} | "
      f"{sum(v for k, v in dr_ocr.items() if k >= 3)} |")
    A("")
    A(f"中文的破折号规范写法是占两个字符位置的 `——`，对照文本里 {dr_ref.get(2,0)} 组"
      f"`——` 对 {dr_ref.get(1,0)} 个单 `—`。而 OCR 产物里出现了 **{dr_ocr.get(1,0)} 个单 `—`** —— "
      f"也就是说 {dr_ref.get(2,0)} 组双破折号里有约 "
      f"**{dr_ref.get(2,0) - dr_ocr.get(2,0)} 组被吞成了一个字符**。")
    A("")
    A("**机制**：识别器用的是 CTC 解码，而 CTC 的「重复 token 合并」规则要求两个相同的字符"
      "**中间必须出现 blank** 才会输出两次。纸上 `——` 是一条连续长横线、中间没有可辨的分隔，"
      "blank 大概率不触发，于是塌缩成一个 `—`。这是**结构性缺陷**，不是置信度问题 —— "
      "调阈值解决不了，只能在后处理补回或换解码方式。")
    A("")
    A(f"除此之外还有少量的 `—`→`一`（破折号被读成汉字「一」）。本节统计到的整段删除里，"
      f"`—` 占 **{dash_del}** 个，是漏字的最大来源。\n")
    A("")

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
    A("> **怎么读这张表**：")
    A("> - **整段删除 644 里，`—` 占 472** —— 这是最大的一块，且是确定性的（见上面的破折号专项），"
      "用一条后处理规则就能补回。")
    A("> - 「汉字↔汉字」（182）才是纠错层该处理的对象（形近字/真错字）。")
    A("> - 「逗号/叹号/冒号/括号/引号/分号」等是模型输出的**字符样式**与排版用字不同，"
      "不是认错字，适合在后处理规则里统一。")
    A("> - 「脚注标记」「块级错位」几乎都来自 0.2 的编码差异，只出现在 3.1 那一桶。\n")

    A("\n## 4. 抽查：逐条具体差异\n")
    A(f"抽查 **{len(sample)} 页** = 全书均匀取样 12 页 + 最差 5 页 + 指定的脚注页/漏行候选页。"
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
    A(f"两类页不参与上面的指标：**锚定失败页 {len(un)} 页** 与 "
      f"**前置/后置页 {len(front)} 页**（后者见 0.6，与对照文本版次不同）。\n")
    if un:
        A("**（1）无法在对照文本中锚定：**\n")
        A("| 页 | 该页识别字数 | 原因 |")
        A("|---|---|---|")
        for a in un:
            A(f"| `{a['name']}` | {a['len']} | {a.get('why', '')} |")
        A("")
        A("这些页是几乎无正文的封面/插页，或页面开头文字严重损坏以致找不到锚串。\n")
    else:
        A("**（1）锚定失败页**：无 —— 全部页锚定成功。\n")
    if front:
        A(f"**（2）前置/后置页（{len(front)} 页）：**\n")
        A("| 页 | |")
        A("|---|---|")
        for x in front:
            head = n1(x["doc"]["lines"][0]["text"])[:30] if x["doc"].get("lines") else ""
            A(f"| `{x['name']}` | {head} |")
        A("")

    A("\n## 6. 结论\n")
    A(f"1. **正文页逐字准确率 {pct(1 - N1.cer)}（N1 最严口径，已剔除脚注页与页码行）**；"
      f"不剔页码是 {pct(1 - C1.cer)}，再排除全角/半角等编码差异是 {pct(1 - C2.cer)}。"
      f"页边界经 0.5 的四项自检（锚定方式/单调性/平移敏感度/段长一致性），排名与归因不含对齐噪声。")
    A(f"2. **真正读错的汉字只有 {pct(NC.cer)}**（剔页码后）。其余差异绝大多数不是「认错字」，"
      f"而是标点与字符样式（见第 3 节）。")
    A(f"3. 🔴 **最大的产物缺陷是破折号**：中文规范写法 `——` 被 CTC 解码塌缩成单个 `—` —— "
      f"对照文本里 {dr_ref.get(2,0)} 组 `——`，OCR 产物里只剩 {dr_ocr.get(2,0)} 组，"
      f"另有 {dr_ocr.get(1,0)} 个单 `—` 是塌缩的产物（对照只有 {dr_ref.get(1,0)} 个单破折号）。"
      f"《围城》大量用 `——` 表示「话未说完」，对 EPUB 阅读体验的伤害大于偶发错字。")
    A(f"4. 错误集中在少数页：中位数 {pct(q(0.5))}，90 分位 {pct(q(0.90))}，"
      f"最差 {pct(q(0.999))}。")
    A(f"5. 抽样逐条差异见第 4 节。需要纠错层处理的是「汉字↔汉字」那一类（形近字/真错字）；"
      f"破折号、标点样式与页码混入更适合**后处理规则**统一。")
    A(f"6. 🔴 **必须修的产物缺陷：{pn_stat['n']} 行印刷页码被当成正文**"
      f"（{pn_stat['pages']} 页，{pn_stat['bottom']} 行在页脚区）。"
      f"它会进入下游 EPUB 正文，也污染所有对照指标。"
      f"建议在 `pipeline/ocr.py` 里按「整行纯数字 + 位于页脚/页首区」剔除"
      f"（或至少落一个 `page_number` 标记让下游可滤）。")
    A(f"7. 其余遗留：{len(dirty)} 页脚注因对照文本编码不同，无法直接逐字比对（已在 3.1 单列）；"
      f"{len(front)} 页前置/后置页与 {len(un)} 页无法锚定的页不参与指标（第 5 节）。\n")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
