# -*- coding: utf-8 -*-
"""形近字纠错：评测 + 全书扫描复核清单（**零推理**，只读产物）。

两件事
------
1. **评测**：用冻结评测集 `eval/glyph_confusion_set.json` 给方法打分。
2. **扫描**：把规则跑在**整本书**上产出复核清单 —— 这是真正降低校对成本的一步：
   改动清单本身就是候选错误清单，逐条复核原图即可（台账外的可能是真错，也可能是误改）。

为什么必须先有评测集
--------------------
形近字错读是**高置信度错误**（conf 0.983~0.996），任何基于 confidence 的守卫对它完全
失明；唯一有效信号是 top1/top2 的**概率间距**（产物里的 `top2`）。
而且这类错字读起来是通的（抡/抢、己/已），肉眼也难发现。

**主指标 = 净收益 = 改对 − 待复核。** 只测召回会被「无脑替换」骗过去：
实测 `blind_swap` 改对 28 但待复核 148、净 −120 —— 与语言模型路线的 −660 同一形态。

支持两代产物口径
----------------
- `top2`（现口径）：**无门槛**，每个解码字符一条，与 `text` 逐字对齐。
- `alt_candidates`（旧口径）：带门槛（`p2/p1 > 0.05`）的"摇摆位置"。

⚠️ **采集门槛必须低于评估阈值**，否则扫描表的下半段是假的：
拿旧口径产物扫 0.03，结果与 0.05 相同是**同义反复**（0.05 以下根本没采），
而它看起来完全正常（单调、无拐点）。本脚本会把它标成
`⚠ 低于采集门槛，与上一行同源`，不让它冒充结论。

用法
----
    ./.venv/Scripts/python.exe eval/score_glyph.py --products eval/products
    ./.venv/Scripts/python.exe eval/score_glyph.py --products eval/products --scan-thr 0.05
    ./.venv/Scripts/python.exe eval/score_glyph.py --products orign_pdf/textlines \\
        --set <本书自己的冻结台账>.json

⚠️ **评测集是「一本书一份」的**
-----------------------------
`eval/glyph_confusion_set.json` 现有那 34 条锚定的是**《爱尔兰神话全书》**的产物
（`token/occ/off` + `ref_line_text` 逐条锚在具体行上）。换一本书它必然**一条都定位不到** ——
所以本脚本在「目标页一条都没命中」时**直接拒绝运行**（exit 2），而不是打一张
`覆盖率 0/34` 的空表让人误读成"这本书没有形近字错误"。
本书要评测就新建一份台账（格式见 `docs/ocr-correction-design.md` §8）。

输出四段
--------
- **A 覆盖率诊断**（与方法无关）：正确字出现在 top-2 候选里的比例、比值分布。
- **B 阈值扫描**（**全书口径**）：同一规则扫不同阈值 → 改对 / 待复核 取舍曲线。
- **C 标定基线**：identity / blind_swap，用来看清指标量级。
- **D 全书复核清单**：全书所有改动 + 上下文，标出台账内/外，
  写成 `review_book.json` 与 `review_book.md` 供逐条人工判定。

⚠️ 「待复核」不等于误改
----------------------
对照集只能标出「**不是已知正样本**」的位置，**不能**保证它们真的正确。
实测第一次跑，唯一的"误改"（page_078「坠人了梦乡」）复核下来是**台账漏掉的真错**
（放大 6× 见带竖直起笔的长捺 = 入；「坠入梦乡」是固定搭配）。
⇒ 清单里台账外的每一条都要看原图再判；改动清单本身就是**候选错误清单**。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from linefix import common as C  # noqa: E402

SET_PATH = _ROOT / "eval" / "glyph_confusion_set.json"


# ---------------------------------------------------------------------------
# 定位：用 (token, occ, off) 找到"错字在行内第几个字"，**不依赖行号空间**
# ---------------------------------------------------------------------------
def locate(rec: Dict[str, Any], doc: Dict[str, Any]) -> Dict[str, Any]:
    """在产物里定位这条记录。返回 `{ok, line_idx, line, char_index, why}`。"""
    lines = doc.get("lines") or []
    token, occ, off = rec["token"], rec.get("occ", 1), rec.get("off", 0)

    cands: List[int] = []
    for i, ln in enumerate(lines):
        txt = ln.get("text") or ""
        pos, seen = -1, 0
        while True:
            pos = txt.find(token, pos + 1)
            if pos < 0:
                break
            seen += 1
            if seen == occ:
                break
        if seen == occ and pos >= 0:
            cands.append(i)

    if not cands:
        return {"ok": False, "why": f"产物里找不到 {token!r}（第 {occ} 次）"}

    # 有 ref_line_text 就以它为准：**只比 token + 字符值是不够的** ——
    # 实测踩过：锚点生成时若只按"第 N 个字是某个字"匹配，会锚到**另一行**
    # （那行同位置恰好也是同一个字），而 `got == wrong` 这个弱校验会放过去。
    ref_text = rec.get("ref_line_text")
    if ref_text:
        exact = [i for i in cands if (lines[i].get("text") or "") == ref_text]
        if exact:
            pick = exact[0]
        elif any((lines[i].get("text") or "") == ref_text for i in range(len(lines))):
            pick = next(i for i in range(len(lines))
                        if (lines[i].get("text") or "") == ref_text)
            cands = [pick]
        else:
            return {"ok": False,
                    "why": f"锚点失配：找不到 ref_line_text 对应的行（token={token!r}）"}
    elif len(cands) > 1:
        ref = rec.get("ref_line_id")
        pick = next((i for i in cands if i + 1 == ref), cands[0])
    else:
        pick = cands[0]

    ln = lines[pick]
    txt = ln.get("text") or ""
    ci = txt.find(token) + off
    got = txt[ci: ci + 1]
    if got != rec["wrong"]:
        return {"ok": False, "why": f"定位到了但第 {ci} 字是 {got!r}，不等于错字 {rec['wrong']!r}"}
    return {"ok": True, "line_idx": pick, "line": ln, "char_index": ci,
            "ambiguous": len(cands) > 1, "why": ""}


def iter_alts(line: Dict[str, Any]):
    """把两种产物口径归一成 `{"i","t","c1","p1","c2","p2"}`。

    🔴 2026-09-26 起**实现搬到 `linefix.common.iter_alts`**（唯一实现层）——
    `linefix.recheck` 也要读同一份口径，读产物的口径只允许有一份实现。
    这里只做转发，签名与行为不变（完整说明见 common 里那份的 docstring）。
    """
    return C.iter_alts(line)


def min_observed_ratio(docs: Dict[str, Dict[str, Any]]) -> Optional[float]:
    """产物的**采集下界**：优先读 `provenance.top2_floor`，读不到才用实测最小 p2/p1。

    用途：阈值扫描若扫到比它还低，得到的"结果相同"是**同义反复**（低于下限的地方
    根本没采），不是"下限稳定"。这条坑本项目踩过一次，所以做成硬告警而不是靠人记。

    🔴 为什么必须优先信**声明值**而不是实测最小值：声明值才是契约。
    实测最小值只是"恰好没人在下限附近"的偶然结果 —— 用它当界会把
    **有效的低阈值误判成同义反复**（例如声明的下限是 1e-3，但数据里最小只到 0.02，
    于是一路标到 0.02 为止都是"⚠ 同义反复"，而 0.005 其实完全有效）。
    反过来若实测值**低于**声明值，说明采集口径与声明不符，也要报出来。
    """
    declared: Optional[float] = None
    for doc in docs.values():
        v = (doc.get("provenance") or {}).get("top2_floor")
        if v is not None:
            declared = float(v) if declared is None else min(declared, float(v))

    observed: Optional[float] = None
    for doc in docs.values():
        for ln in doc.get("lines") or []:
            for a in iter_alts(ln):
                p1, p2 = a.get("p1"), a.get("p2")
                if p1:
                    r = (p2 or 0) / p1
                    observed = r if observed is None else min(observed, r)

    if declared is not None and observed is not None and observed < declared - 1e-9:
        print(f"  🔴 实测算术最小比值 {observed:.6f} **低于**声明的采集下限 "
              f"{declared:.6f} —— 采集口径与声明不符，先查清再读这张表。")
    if declared is not None:
        return declared
    return observed

def alt_at(line: Dict[str, Any], char_index: int) -> Optional[Dict[str, Any]]:
    """取产物里该字符位置的 top-2 条目（没有就 None）。"""
    for a in iter_alts(line):
        if a.get("i") == char_index:
            return a
    return None


def pair_chars(records: Sequence[Dict[str, Any]]) -> Dict[str, Tuple[str, str]]:
    """{组名: (wrong, right)}。"""
    out: Dict[str, Tuple[str, str]] = {}
    for r in records:
        out.setdefault(r["group"], (r["wrong"], r["right"]))
    return out


def count_chars(docs: Dict[str, Dict[str, Any]], chars: set) -> Dict[str, int]:
    c: Dict[str, int] = {}
    for doc in docs.values():
        for ln in doc.get("lines") or []:
            for ch in (ln.get("text") or ""):
                if ch in chars:
                    c[ch] = c.get(ch, 0) + 1
    return c


def context_of(doc: Dict[str, Any], line_idx: int, ci: int, span: int = 12) -> str:
    txt = (doc["lines"][line_idx].get("text") or "")
    return f"{txt[max(0, ci - span):ci]}【{txt[ci]}】{txt[ci + 1:ci + 1 + span]}"


def collect_rule_hits(docs: Dict[str, Dict[str, Any]], pairs, thr: float) -> List[Dict[str, Any]]:
    """规则命中项（全书）。规则见 `m_alt_ratio`：单向、纯图像后验。"""
    items: List[Dict[str, Any]] = []
    for page in sorted(docs):
        doc = docs[page]
        for li, ln in enumerate(doc.get("lines") or []):
            for a in iter_alts(ln):
                for w, r in pairs.values():
                    if a.get("c1") == w and a.get("c2") == r and a.get("p1"):
                        ratio = (a.get("p2") or 0) / a["p1"]
                        if ratio > thr:
                            ci = a["i"]
                            items.append({
                                "page": page, "line_id": li + 1, "char_index": ci,
                                "before": (ln.get("text") or "")[ci], "new": r,
                                "group": f"{w}→{r}", "ratio": round(ratio, 4),
                                "p1": a.get("p1"), "p2": a.get("p2"),
                                "bbox": ln.get("bbox"),          # 供裁原图复核
                                "context": context_of(doc, li, ci)})
    return items


# ---------------------------------------------------------------------------
# 方法
# ---------------------------------------------------------------------------
Change = Tuple[str, int, int, str]          # (page, line_idx, char_index, new_char)


def m_identity(docs, records, pairs, **kw) -> List[Change]:
    return []


def m_blind_swap(docs, records, pairs, **kw) -> List[Change]:
    """最蠢基线：把全书的 wrong 一律改成 right。用来标定误改的量级。"""
    out: List[Change] = []
    for grp, (w, r) in pairs.items():
        for page, doc in docs.items():
            for i, ln in enumerate(doc.get("lines") or []):
                for ci, ch in enumerate(ln.get("text") or ""):
                    if ch == w:
                        out.append((page, i, ci, r))
    return out


def m_alt_ratio(docs, records, pairs, thr: float = 0.05, **kw) -> List[Change]:
    """纯图像后验：该位置 top1 是错字、top2 是正确字，且 p(对)/p(错) > thr → 改成正确字。

    **单向**（只按 错→对）。反方向会去改本来就对的地方：合法「自己」处
    top1=己/top2=已，双向规则会把「自己」改成「自已」。
    不含任何语言先验 —— 这是本项目结论里唯一被允许的证据源。
    """
    return [(x["page"], x["line_id"] - 1, x["char_index"], x["new"])
            for x in collect_rule_hits(docs, pairs, thr)]


def score(changes: Sequence[Change], records: Sequence[Dict[str, Any]],
          docs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """按位置去重后计分。

    改对 = 命中正样本且新字正确；**其余任何改动都进"待复核"**
    （对照集 = 非正样本位置）。
    ⚠️ "待复核"只是**待判定**：实测唯一的"误改"复核下来是台账漏收的真错，
    所以这里把全部非正样本改动的上下文一起返回，供逐条判定。
    """
    pos_any = {(r["page"], r["char_index"]): r for r in records}
    seen: Dict[Tuple[str, int, int], str] = {}
    for page, li, ci, new in changes:
        seen[(page, li, ci)] = new

    fixed: List[Dict[str, Any]] = []
    other: List[Dict[str, Any]] = []
    for (page, li, ci), new in sorted(seen.items()):
        r = pos_any.get((page, ci))
        if r is not None and new == r["right"]:
            fixed.append({"id": r["id"], "group": r["group"]})
        else:
            other.append({"page": page, "line_id": li + 1, "char_index": ci, "new": new,
                          "known_id": r["id"] if r else None,
                          "before": (docs[page]["lines"][li]["text"] or "")[ci],
                          "context": context_of(docs[page], li, ci)})
    return {"changes": len(seen), "fixed": len(fixed), "fixed_detail": fixed,
            "fp": len(other), "other_detail": other, "net": len(fixed) - len(other)}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def load_docs(products: Path, pages: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    docs: Dict[str, Dict[str, Any]] = {}
    for p in pages:
        d = C.load_json(products / f"{p}.textlines.json")
        if isinstance(d, dict) and "lines" in d:
            docs[p] = d
    return docs


def write_review(items: List[Dict[str, Any]], thr: float, n_known: int,
                 json_path: Path, md_path: Path, products: Optional[Path] = None) -> None:
    unknown = [x for x in items if x["known_id"] is None]
    # `products` 写进清单：`review_sheet.py` 要拿**同一批**产物去取行文本。
    # 不给它就只剩「<清单父目录>/products」这条路径猜测 —— 本项目产物在
    # `orign_pdf/textlines`，猜出来必错（表现只是"贴图没有裁片"，不报错）。
    C.dump_json(json_path, {"thr": thr, "total": len(items), "known": n_known,
                            "unknown": len(unknown),
                            "products": str(products) if products else None,
                            "items": items})
    lines = [
        f"# 全书纠错复核清单（thr={thr}）",
        "",
        "规则：`top1=错字 & top2=正确字 & p(对)/p(错) > thr`（**单向**，纯图像后验，零推理）。",
        "",
        f"- 全书改动 **{len(items)}** 处：台账内（已知真错）**{n_known}** 处、"
        f"**台账外 {len(unknown)}** 处",
        "- ⚠️ 台账外**不等于误改**：实测台账外的第一条（page_078「坠人了梦乡」）"
        "复核下来是台账漏收的真错。**逐条看原图再定**。",
        "- 复核后请回填到 `eval/glyph_confusion_set.json`：真错就加进去，"
        "误改就据此调整阈值/加约束。",
        "",
        "| # | 页 | 行 | 位置 | 改动 | p(对)/p(错) | 台账 | 上下文 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, x in enumerate(items, 1):
        lines.append(f"| {i} | {x['page']} | {x['line_id']} | {x['char_index']} | "
                     f"{x['before']}→{x['new']} | {x['ratio']:.3f} | "
                     f"{x['known_id'] or '**台账外**'} | {x['context']} |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="形近字评测 + 全书扫描复核清单（零推理）")
    ap.add_argument("--products", required=True, help="产物目录（*.textlines.json）")
    ap.add_argument("--set", default=str(SET_PATH), help="评测集 JSON")
    ap.add_argument("--out", default=None, help="报告输出（默认 <products>/../report_glyph.json）")
    ap.add_argument("--scan-thr", type=float, default=0.05,
                    help="全书扫描阈值（0 = 只评测不扫描）")
    args = ap.parse_args(argv)
    C.force_utf8_stdout()

    spec = C.load_json(args.set)
    if not spec:
        print(f"✗ 读不到评测集：{args.set}")
        return 2
    records: List[Dict[str, Any]] = spec["records"]
    pairs = pair_chars(records)
    products = Path(args.products)

    # 扫描要覆盖产物目录里的**所有**页；评测只关心含目标页的那几页
    all_pages = sorted(p.name.replace(".textlines.json", "")
                       for p in products.glob("*.textlines.json"))
    docs = load_docs(products, all_pages)
    if not docs:
        print(f"✗ {products} 里没有产物")
        return 2
    tgt = sorted({r["page"] for r in records})
    hit = sum(1 for p in tgt if p in docs)
    print(f"评测集 : {spec['name']}（{len(records)} 条，{len(pairs)} 组）")
    print(f"产物   : {products}  全书 {len(docs)} 页有产物"
          f"（目标页命中 {hit}/{len(tgt)}）")
    if hit == 0:
        # 🔴 评测集是**按书锚定**的（token/occ/off + ref_line_text）。一页都对不上，
        #    几乎一定是「拿 A 书的台账去评 B 书的产物」。继续跑只会打一张
        #    `覆盖率 0/34 / 改对 0 / 净收益 0` 的表，看起来像"这本书没有形近字错误"。
        print(f"\n✗ 评测集的目标页与产物**一页都对不上** —— 这不是"
              f"「本书没有形近字错误」，而是「台账不是这本书的」。")
        print(f"  台账锚定的是：{(spec.get('book') or '（未标注）')}")
        print(f"  请用 --set 指向本书自己的台账（格式见 docs/ocr-correction-design.md §8）。")
        return 2

    # ---------------- A 覆盖率诊断（与方法无关） ----------------
    print("\n" + "=" * 72)
    print("A 覆盖率诊断：正确字是否出现在 top-2 候选里（决定天花板）")
    cov: List[Dict[str, Any]] = []
    for r in records:
        doc = docs.get(r["page"])
        if doc is None:
            cov.append({**r, "located": False, "why": "该页无产物"})
            continue
        loc = locate(r, doc)
        if not loc["ok"]:
            cov.append({**r, "located": False, "why": loc["why"]})
            continue
        a = alt_at(loc["line"], loc["char_index"])
        has = bool(a) and r["right"] in (a.get("c1"), a.get("c2"))
        cov.append({**{k: r[k] for k in ("id", "page", "group", "kind", "judge", "wrong", "right")},
                    "located": True, "line_id": loc["line_idx"] + 1,
                    "char_index": loc["char_index"],
                    "has_alt": bool(a), "right_in_alt": has,
                    "c1": (a or {}).get("c1"), "p1": (a or {}).get("p1"),
                    "c2": (a or {}).get("c2"), "p2": (a or {}).get("p2"),
                    "ratio": round((a or {}).get("p2", 0) / (a or {}).get("p1", 1), 4)
                    if (a or {}).get("p1") else None})
    n_cov = sum(1 for c in cov if c.get("right_in_alt"))
    n_located = sum(1 for c in cov if c.get("located"))
    print(f"\n定位成功 {n_located}/{len(records)}"
          f"｜正确字在候选里 {n_cov}/{len(records)}（{n_cov / max(1, len(records)):.0%}）")
    for g in pairs:
        rs = [c for c in cov if c["group"] == g]
        h = sum(1 for c in rs if c.get("right_in_alt"))
        rats = [c["ratio"] for c in rs if c.get("ratio") is not None]
        rng = f"{min(rats):.3f}~{max(rats):.3f}" if rats else "—"
        print(f"  {g}: {h}/{len(rs)}   比值 p(对)/p(错) {rng}")

    # 🔴 硬闸门：**一条都定位不到** ⇒ 台账不是这本书的。
    #    注意「目标页命中 34/34」并不足以证明同书 —— 不同书的页名都是 page_NNN，
    #    页码会大面积**假装**命中（实测拿《爱尔兰》台账去评《围城》产物：
    #    目标页名义命中 34/34，但 34 条一条都锚不上）。唯一可信的信号是**定位成功数**。
    if n_located == 0:
        print(f"\n✗ 评测集 {len(records)} 条**一条都没定位到** —— 这不是"
              f"「本书没有形近字错误」，而是「台账锚的不是这批产物」。")
        print(f"  台账锚定的是：{spec.get('book') or '（未标注）'}")
        print(f"  定位失败原因示例：{cov[0].get('why')}")
        print(f"  请用 --set 指向本书自己的台账（格式见 docs/ocr-correction-design.md §8）。")
        return 2
    if n_located < len(records) * 0.5:
        print(f"\n⚠ 定位成功率只有 {n_located}/{len(records)} —— 下面的扫描表只覆盖"
              f"能锚上的那部分，别当成全书结论。")

    # ---------------- B 阈值扫描（全书口径） ----------------
    print("\n" + "=" * 72)
    print("B 阈值扫描：规则 = top1是错字 & top2是正确字 & p(对)/p(错) > thr（单向）")
    chars = {c for pair in pairs.values() for c in pair}
    cnt = count_chars(docs, chars)
    print(f"全书混字字频：{'  '.join(f'{k}×{v}' for k, v in sorted(cnt.items()))}")
    print(f"对照集规模：{sum(cnt.values()) - len(records)} 个位置（非正样本的混字位置）")

    # 采集下界只由 `min_observed_ratio()` 一处算（它内部优先信 provenance 里的声明值）——
    # 这里再单独取一份 `declared` 就是"同一逻辑两份实现"，两处迟早对不上。
    min_r = min_observed_ratio(docs)
    declared = next((((d.get("provenance") or {}).get("top2_floor"))
                     for d in docs.values()
                     if (d.get("provenance") or {}).get("top2_floor") is not None), None)
    n_schema_new = sum(1 for d in docs.values() for ln in (d.get("lines") or [])
                       if ln.get("top2"))
    n_schema_old = sum(1 for d in docs.values() for ln in (d.get("lines") or [])
                       if not ln.get("top2") and ln.get("alt_candidates"))
    print(f"产物口径：top2（当前口径）{n_schema_new} 行｜alt_candidates（旧，带门槛）"
          f"{n_schema_old} 行｜**声明的采集下限** {declared if declared is not None else '（未声明）'}"
          f"｜实测最小比值 {min_r if min_r is None else f'{min_r:.4f}'}")
    if min_r is not None and min_r > 0:
        src = "provenance 声明值" if declared is not None else "实测最小值（产物未声明下限）"
        print(f"  ⚠ 采集下界 = {min_r:.6g}（{src}）⇒ **扫描阈值低于它的行没有意义**，"
              f"\n    「结果与上一行相同」是同义反复（那批数据根本没采），不是下限稳定。")

    print(f"\n{'thr':>7} {'改动':>7} {'改对':>6} {'待复核':>8} {'净收益':>7}  说明")
    sweep: List[Dict[str, Any]] = []
    keep: Dict[float, Dict[str, Any]] = {}
    prev_below = False
    floor_row: Optional[Dict[str, Any]] = None
    # 扫到**声明的采集下限**为止 —— 再往下就是同义反复。下限附近这几档
    # （0.005 / 0.002 / 0.001）正是"把采集门槛降下来"才换得到的区间。
    _sweep = [1.0, 0.5, 0.3, 0.2, 0.1, 0.05, 0.03, 0.01, 0.005, 0.002, 0.001]
    for thr in _sweep:
        res = score(m_alt_ratio(docs, records, pairs, thr=thr), records, docs)
        below = min_r is not None and thr < min_r - 1e-9
        rec_row = {"thr": thr, "below_capture_threshold": below,
                   **{k: res[k] for k in ("changes", "fixed", "fp", "net")}}
        sweep.append(rec_row)
        keep[thr] = res
        if below and not prev_below:
            note = (f"← 实际截止到采集门槛 {min_r:.4f}：这一行仍用满全部可用数据，"
                    f"再往下就没有数据了")
            floor_row = rec_row
        elif below:
            same = floor_row is not None and all(
                rec_row[k] == floor_row[k] for k in ("changes", "fixed", "fp", "net"))
            note = ("⚠ 与上一行完全相同 —— 低于采集门槛的数据**根本不存在**，"
                    "这是同义反复，不是\"下限稳定\"（要扫更低就得先放开采集门槛）")
            if not same:
                note = (f"🔴 与门槛行不同！说明采集下限并非 {min_r:.4f} —— "
                        f"先把口径查清再解读这张表")
        else:
            note = ""
        # `%g` 而不是 `%.2f` —— 否则 0.005 / 0.002 / 0.001 会被打成同一个 "0.00"，
        # 表格看着像重复行，实际上它们各自有效。
        print(f"{thr:>7g} {res['changes']:>7} {res['fixed']:>6} {res['fp']:>8} "
              f"{res['net']:>7}  {note}")
        prev_below = below
    b_rows = [s for s in sweep if s["below_capture_threshold"]]
    if len(b_rows) > 1:
        print(f"\n  ⇒ 这张表的有效行只到 {b_rows[0]['thr']:.2f} 为止（共 {len(sweep) - len(b_rows) + 1} 行）。"
              f"其余 {len(b_rows) - 1} 行是同一个数的重复。")

    # ---------------- C 标定基线 ----------------
    print("\n" + "=" * 72)
    print("C 标定基线（看清指标量级，不是候选方案）")
    baselines = []
    for name, fn in (("identity 不改", m_identity), ("blind_swap 无脑替换", m_blind_swap)):
        res = score(fn(docs, records, pairs), records, docs)
        baselines.append({"method": name, **{k: res[k] for k in ("changes", "fixed", "fp", "net")}})
        print(f"  {name:<22} 改动 {res['changes']:>5}  改对 {res['fixed']:>3}"
              f"  待复核 {res['fp']:>5}  净收益 {res['net']:>6}")

    # ---------------- D 全书复核清单 ----------------
    review_json = review_md = None
    if args.scan_thr > 0:
        hits = collect_rule_hits(docs, pairs, args.scan_thr)
        idx = {(x["page"], x["char_index"]): x["id"] for x in records}
        for x in hits:
            x["known_id"] = idx.get((x["page"], x["char_index"]))
        hits.sort(key=lambda x: (x["known_id"] is None, x["page"], x["line_id"]))
        n_known = sum(1 for x in hits if x["known_id"])
        review_json = products.parent / "review_book.json"
        review_md = products.parent / "review_book.md"
        write_review(hits, args.scan_thr, n_known, review_json, review_md, products)
        print("\n" + "=" * 72)
        print(f"D 全书复核清单（thr={args.scan_thr}）：改动 {len(hits)} 处"
              f"｜台账内 {n_known}｜**台账外 {len(hits) - n_known}（需逐条看原图）**")
        for x in hits[:16]:
            print(f"  {x['page']} L{x['line_id']:<3} [{x['known_id'] or '台账外':<8}] "
                  f"{x['before']}→{x['new']}  r={x['ratio']:.3f}  {x['context']}")
        if len(hits) > 16:
            print(f"  … 其余 {len(hits) - 16} 处见清单文件")

    out = Path(args.out) if args.out else products.parent / "report_glyph.json"
    C.dump_json(out, {"set": spec["name"], "products": str(products),
                      "pages_scanned": len(docs), "coverage": cov, "sweep": sweep,
                      "baselines": baselines, "char_counts": cnt,
                      "review_json": str(review_json or ""), "review_md": str(review_md or "")})
    print(f"\n报告 -> {out}")
    if review_md:
        print(f"清单 -> {review_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
