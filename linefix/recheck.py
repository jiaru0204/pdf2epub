# -*- coding: utf-8 -*-
"""linefix.recheck —— 原图二次后验：对可疑位置做**定向修复**（或判定该位置本无字）。

为什么是"定向修复"而不是"第二意见投票"
--------------------------------------
2026-09-26 在 34 条形近字台账上实测（`rec_arbitrate.py` / `rec_risk.py`）：

| 结果 | 条数 |
|---|---|
| 原图裁行重识别**直接读对** | **20 / 34（59%）** |
| 仍是同一个错字 | 13 |
| 长度变化·整行作废 | 1 |

并且这 20 条**全部是"只改目标位 1 处"**，**0 连带改动**；13 条一字未动。
按组看：`人/入` 8/9、`已/己` 13/16 —— 而 **`抢/抡` 0/8**（该字对两路都难，另想办法）。
conf 全程 0.97~0.999 ⇒ **置信度分不出对错**，"按 conf 门控"这条路再次确认无效。

所以本模块的验收规则是**结构式**的，不是打分式的：

    长度不变 且 逐位 diff 恰好只有目标位一处  → `改字`
    长度 = n-1 且 少掉的正是目标字            → `删字`
    其余（长度差 >1 / 多点差异 / 取不到证据）  → 不改，记 `证据不足·待核`

🔴 **判定 ≠ 采纳**。采纳另有两条**内容保全**原则（`should_adopt`，用户 2026-09-26 定）：
  1. **提升小的不用** —— 标点全角半角（那 507 条摇摆）、装饰噪点、空白多寡，影响面小
     却要动原文，一律不采纳。
  2. **对原文内容有损害的不用** —— **任何删除都不采纳**：删一个字就是不可逆地丢内容，
     而"这字是不是幻觉"目前只有像素代理量（墨量比）做证据，不足以为删除背书。
  ⇒ 默认 `--adopt cjk`：**只采纳"汉字 → 汉字"的定向替换**。
  `--adopt all` 才连删除一起采纳（**不要在无人复核时用**）；`--adopt none` 只出报告。

候选位置（= 原来说的"混淆对表"）**不写死**，直接从产物现取：
  · `blank`：`top2` 的第二候选是空白 —— 模型觉得"这儿可能没字"（全书 355 个）
  · `cjk`  ：第二候选也是汉字 —— 形近字摇摆（全书 346 个）
⇒ #6「数据驱动扩充混淆对表」被它取代：**位置从数据取、方向由重识别定**，
   不再需要人工维护"哪一对往哪边改"的表格（人工表只留 4 组、且必须单向）。

用法
----
    ./.venv/Scripts/python.exe -m linefix.recheck --pages <页目录> --products <产物目录>
        [--targets blank,cjk] [--out <目录>] [--ratio-min 0] [--apply] [--limit N] [--dry-run]

默认输出 `<pages>/../linefix/recheck/`：`report.json` + `report.md` + `corrections.json`；
加 `--apply` 才写 `corrected/*.textlines.json`（**非破坏性**，原产物一律不动；
且只写**被改动的页**，`corrected/` 是**部分集**，下游要按页合并）。

退出码：0 正常 / 1 有行的原图裁片失败（那些行**没被复核**）/ 2 参数或目录不可用。

⚠️ 与 `linefix.rescue` 的关系：两者都是"从原图裁框 rec-only 重识别"，但
   **目标与验收不同**（rescue 治"有框无行/低置信度整行"；本模块治"行内某一个字"）。
   共用 `linefix.common` 的引擎构造、字级定位与 IO，不另写一份。
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

from . import common as C


def is_cjk(s: Any) -> bool:
    """单个汉字的粗判（含扩展 A 区）。只用于把摇摆位分成 `blank` / `cjk` 两类。"""
    return isinstance(s, str) and len(s) == 1 and "\u3400" <= s <= "\u9fff"


#: 墨迹判定：目标字位的墨量 / 本行各字位墨量的中位数，低于它视为"这里没字"。
#: 🔴 **它不是一个可以照搬的常数**：0.25 是先验取值，随后被实测检验 ——
#:    本书 `blank` 类 p25=0.21 / p50=0.39，而 `cjk` 类 p5=0.50（两类分布确实分开，
#:    说明"墨量"这个代理量有区分力），但**具体切在哪必须按书重标**：
#:    运行后看报告里的 `ink_quantiles_by_kind`，再决定 `--ink-cut`。
INK_CUT = 0.25
INK_GRAY_CUT = 40          # 灰度差小于它的算白纸（去扫描底噪）


def _paths(args) -> Dict[str, Path]:
    pages = Path(args.pages)
    root = Path(args.out) if args.out else pages.parent / "linefix"
    rc = root / "recheck"
    return {
        "pages": pages,
        "products": Path(args.products),
        "root": root,
        "out": rc,
        "corrected": rc / "corrected",
        "report_json": rc / "report.json",
        "report_md": rc / "report.md",
        "corrections": rc / "corrections.json",
    }


# ---------------------------------------------------------------------------
# 候选位置：从产物里现取（不维护人工"混淆对表"）
# ---------------------------------------------------------------------------
def collect_targets(products: Path, pages: Sequence[str], kinds: Sequence[str],
                    ratio_min: float, limit: int,
                    stat: Dict[str, int]) -> List[Dict[str, Any]]:
    """扫产物，列出**要复核的位置**。一行多个候选位只记一次行，但每个位都留。

    🔴 这就是原来那件事（**#6「数据驱动扩充混淆对表」**）的替代做法：
       **不维护"哪一对往哪边改"的人工表**，候选位置直接从数据现取
       （`top2` 的每个摇摆位），**方向交给原图重识别定**。
       人工表只留 4 组、还必须单向（`blind_swap` 双向误改 1520 就是教训）；
       而这里 `己→已` 出现 93 次也不会出问题 —— 因为没人预设方向。
    """
    out: List[Dict[str, Any]] = []
    for page in pages:
        doc = C.load_json(products / f"{page}.textlines.json")
        if not isinstance(doc, dict) or "lines" not in doc:
            C.inc(stat, "产物缺失/坏")
            continue
        for ln in doc.get("lines") or []:
            txt = ln.get("text") or ""
            for a in C.iter_alts(ln):
                i = int(a.get("i", -1))
                if not (0 <= i < len(txt)):
                    C.inc(stat, "下标越界")
                    continue
                c1, c2 = txt[i], a.get("c2")
                p1, p2 = float(a.get("p1") or 0), float(a.get("p2") or 0)
                r = (p2 / p1) if p1 > 0 else 0.0
                if r < ratio_min:
                    C.inc(stat, "低于 --ratio-min")
                    continue
                kind = None
                if "blank" in kinds and (c2 == "blank" or (isinstance(c2, str) and not c2.strip())):
                    kind = "blank"
                elif "cjk" in kinds and is_cjk(c1) and is_cjk(c2):
                    kind = "cjk"
                if kind is None:
                    C.inc(stat, "按 --targets 过滤掉")
                    continue
                out.append({"page": page, "line_id": ln.get("line_id"),
                            "char_index": i, "kind": kind, "c1": c1, "c2": c2,
                            "ratio": round(r, 4),
                            "bbox": ln.get("bbox"), "text": txt})
                if limit and len(out) >= limit:
                    return out
    return out


# ---------------------------------------------------------------------------
# 单行：原图二次后验 + 字级定位
# ---------------------------------------------------------------------------
def second_opinion(pages: Path, page: str, bbox: Sequence[float], engine, img_cache,
                   pad: int = 6) -> Optional[Dict[str, Any]]:
    """从**原图**裁这一行重识别，返回 `{text, conf, cols, line_txt_len, gray, x0, y0}`。

    `gray` = 该行裁片的**灰度图**（小，几百 KB；墨量判定在它上面做），
    配合 `x0` 可把页坐标的字位区间平移进裁片坐标。

    ⚠️ 裁框外扩 `pad` 与标定时一致（`.tmp/rec_arbitrate.py` 用 6）——
       换 pad 就是换了输入，实测结论不能直接搬。
    ⚠️ `img_cache` **只允许缓存当前页**：300dpi 页图 3125×4344×3 ≈ 40MB，
       缓存整本会吃到十几 GB。切页时由本函数清掉旧页。
    """
    img = img_cache.get(page)
    if img is None:
        img = C.imread_u(pages / f"{page}.png")
        img_cache.clear()
        img_cache[page] = img
    if img is None:
        return None
    H, W = img.shape[:2]
    x, y, w, h = [int(v) for v in bbox]
    x0, y0 = max(0, x - pad), max(0, y - pad)
    crop = img[y0: min(H, y + h + pad), x0: min(W, x + w + pad)]
    if crop.size == 0:
        return None
    # ⚠️ 引擎异常**不在这里吞** —— 冒到 `main` 的记账处（`重识别异常`）。见 common 的说明。
    txt, conf, cols, L = C.rec_line_word_cols(engine, crop)
    return {"text": txt, "conf": conf, "cols": cols, "line_txt_len": L,
            "gray": cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), "x0": x0, "y0": y0}


def line_char_inks(gray: np.ndarray, spans: Sequence[Sequence[float]]) -> List[Optional[float]]:
    """该行每个字位的墨量均值（灰度差 > INK_GRAY_CUT 的像素才算墨）。"""
    vals: List[Optional[float]] = []
    for a, b in spans:
        a, b = int(max(0, a)), int(min(gray.shape[1], b))
        if b - a < 3:
            vals.append(None)
            continue
        ink = 255.0 - gray[:, a:b].astype(np.float32)
        ink[ink < INK_GRAY_CUT] = 0.0
        vals.append(float(ink.mean()))
    return vals


def should_adopt(verdict: str, kind: str, policy: str) -> Tuple[bool, str]:
    """**采纳策略**（纯函数，由 smoke 的 [N] 节回归）。返回 `(是否采纳, 理由)`。

    用户 2026-09-26 定的口径：
      · **提升小的不用** —— 标点全角半角、装饰噪点、空白多寡这类改动，影响面小却要动原文，
        一律不采纳（那 507 条标点摇摆**不进这条线**）。
      · **对原文内容有损害的不用** —— **任何删除都不采纳**：删一个字就是不可逆地丢内容，
        而"这字是不是幻觉"目前只有像素代理量做证据（墨量比），不足以为删除背书。

    ⇒ 默认策略 `cjk`：**只采纳"汉字 → 汉字"的定向替换**（双路证据 + 逐条看原图复核）。
      `all` 才连删除一起采纳（**不要**在无人复核的情况下用）；`none` 只出报告。
    """
    if verdict == "改字":
        if kind == "cjk":
            return True, "汉字形近字定向替换（双路证据一致）"
        return False, "非汉字类（标点/空白/噪点）：提升小，不动原文"
    if verdict == "删字":
        return policy == "all", "删除字符 = 直接减少原文内容（默认不采纳）"
    if verdict == "疑似无字·待核":
        return False, "删除候选：内容有损风险，默认不采纳"
    return False, "无改动"


def judge(target: Dict[str, Any], so: Dict[str, Any],
          ink_rel: Optional[float], ink_cut: float) -> Dict[str, Any]:
    """判定一个候选位。**纯函数**（由 smoke 的 [M] 节直接喂假数据回归）。

    两类目标的证据分工：
      · `cjk`  ：**结构证据**为主 —— 二次后验只改目标位一处 ⇒ 采纳。
      · `blank`：结构证据说不清时**看墨量** —— 模型保留了这个字，但那个位置根本没墨
                 ⇒ 「疑似无字·待核」（**不自动删**：墨量是像素代理量，会受定位误差影响）。
    """
    old = target["text"]
    new = so.get("text") or ""
    ci = int(target["char_index"])
    kind = target.get("kind")
    ink = "—" if ink_rel is None else f"{ink_rel:.2f}"
    low_ink = ink_rel is not None and ink_rel < ink_cut
    res: Dict[str, Any] = {"old": old[ci:ci + 1], "new": None,
                           "diff": None, "verdict": "", "note": ""}
    if not new:
        res.update(verdict="证据不足·待核", note="二次后验为空")
        return res
    if len(new) == len(old):
        diff = [i for i, (a, b) in enumerate(zip(old, new)) if a != b]
        res["diff"] = diff
        res["new"] = new[ci:ci + 1]
        if not diff:
            if kind == "blank" and low_ink:
                res.update(verdict="疑似无字·待核",
                           note=f"两路都保留该字，但该位墨量 {ink}<{ink_cut}")
                return res
            res.update(verdict="两路一致·不改", note="整行一字未动")
            return res
        if diff == [ci]:
            if kind == "blank" and low_ink:
                res.update(verdict="删字",
                           note=f"二次后验改字且该位墨量 {ink}<{ink_cut}")
                return res
            res.update(verdict="改字", note=f"{old[ci]}→{new[ci]}（仅此一处）")
            return res
        res.update(verdict="证据不足·待核",
                   note=f"同行 {len(diff)} 处差异（非定向）")
        return res

    if len(new) == len(old) - 1:
        # 二次后验少了一个字：确认少掉的**正是目标位**（前面几位必须完全一致）
        k = 0
        while k < min(len(new), len(old)) and old[k] == new[k]:
            k += 1
        if k == ci:
            res["new"] = ""
            res.update(verdict="删字", note=f"二次后验少掉目标字（墨量 {ink}）")
            return res
        res.update(verdict="证据不足·待核", note=f"二次后验少了一个字，但第 {k} 位起就不一致")
        return res

    res.update(verdict="证据不足·待核",
               note=f"长度变了（{len(old)}→{len(new)}），对齐失效")
    return res


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def apply_corrections(products: Path, out_corrected: Path,
                      targets: Sequence[Dict[str, Any]]) -> int:
    """把采纳的修正写成 `corrected/*.textlines.json`（只写被改动的页，非破坏性）。

    ⚠️ 改字的行会挂 `text_corrections` 字段（逐字留痕），并因此**必须**有
       `PRODUCT_SCHEMA` 版本 —— 见 `common.PRODUCT_SCHEMA` 的说明。
    """
    by_page: Dict[str, List[Dict[str, Any]]] = {}
    for t in targets:
        if t.get("adopt"):
            by_page.setdefault(t["page"], []).append(t)
    out_corrected.mkdir(parents=True, exist_ok=True)
    n_pages = 0
    for page, items in sorted(by_page.items()):
        doc = C.load_json(products / f"{page}.textlines.json")
        if not isinstance(doc, dict):
            continue
        lines = doc.get("lines") or []
        for t in items:
            ln = lines[t["line_id"] - 1]
            txt = ln.get("text") or ""
            ci = int(t["char_index"])
            new = (t.get("new") or "") if t["verdict"] == "改字" else ""
            if txt[ci:ci + 1] != t["old"]:
                continue                      # 产物变了，放弃这一条（防错位）
            ln["text"] = txt[:ci] + new + txt[ci + 1:]
            ln.setdefault("text_corrections", []).append(
                {"i": ci, "from": t["old"], "to": new, "by": "linefix.recheck",
                 "ev": t["verdict"]})
        doc["provenance"] = dict(doc.get("provenance") or {})
        # 结构里多了 `text_corrections` ⇒ 顶层 `product_schema` 必须跟上，
        # 否则下游拿"版本 3 的产物"去解读"含 v4 字段的结构"。
        doc["provenance"]["product_schema"] = C.PRODUCT_SCHEMA
        doc["provenance"]["recheck"] = {
            "product_schema": C.PRODUCT_SCHEMA,
            "changes": len(items),          # ⚠️ 本页条数，不是全书总数
        }
        C.write_product_json(out_corrected / f"{page}.textlines.json", doc)
        n_pages += 1
    return n_pages


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="原图二次后验 → 定向修复 / blank 判定")
    ap.add_argument("--pages", required=True, help="页面 PNG 目录")
    ap.add_argument("--products", required=True, help="文本行产物目录（*.textlines.json）")
    ap.add_argument("--out", default=None, help="输出目录（默认 <pages>/../linefix）")
    ap.add_argument("--targets", default="blank,cjk",
                    help="候选位置来源：blank=第二候选是空白；cjk=第二候选也是汉字")
    ap.add_argument("--ratio-min", type=float, default=0.0, help="只处理比值 ≥ 它的位置")
    ap.add_argument("--ink-cut", type=float, default=INK_CUT,
                    help=f"墨量比阈值（相对本行中位字位墨量，默认 {INK_CUT}）")
    ap.add_argument("--adopt", choices=("cjk", "none", "all"), default="cjk",
                    help="采纳策略（默认 cjk=只采纳汉字形近字定向替换；"
                         "none=只出报告；all=连删除一起采纳，**不要**无人复核就用）")
    ap.add_argument("--review", default=None,
                    help="score_glyph 的复核清单（review_book.json）：其中**台账内**条目"
                         "作为已验证改动并入落盘（两路一致地错的那批，二次后验看不见）")
    ap.add_argument("--only-file", default=None,
                    help="人工复核白名单 JSON：[{'page','line_id','char_index'}, ...]，"
                         "只有列在内的采纳项才落盘（不给 = 全部采纳项落盘）")
    ap.add_argument("--apply", action="store_true",
                    help="按采纳策略写出修正产物（默认只出报告）")
    ap.add_argument("--backend", default="auto", help="auto=沿用配置文件；否则强制三段覆盖")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少个候选位（抽样试跑）")
    ap.add_argument("--dry-run", action="store_true", help="只列候选，不构造引擎")
    args = ap.parse_args(argv)
    C.force_utf8_stdout()

    P = _paths(args)
    kinds = [s.strip() for s in args.targets.split(",") if s.strip()]
    pages = C.list_pages(P["pages"])
    if not pages:
        print(f"✗ 页图目录里没有 page_*.png：{P['pages']}")
        return 2

    stat: Dict[str, int] = {}
    targets = collect_targets(P["products"], pages, kinds, args.ratio_min,
                              args.limit, stat)
    # 摇摆对的频次（"数据驱动的候选池"长什么样，一眼可见）
    pair_counts: Dict[str, int] = {}
    for t in targets:
        C.inc(pair_counts, f"{t['c1'] or '␠'}→{t['c2']}")
    print(f"页 {len(pages)}，候选位置 {len(targets)} 个（targets={kinds}）")
    for k, v in sorted(stat.items(), key=lambda kv: -kv[1]):
        print(f"  · {k}: {v}")
    print("  摇摆对 top10：" + "  ".join(
        f"{k}×{v}" for k, v in sorted(pair_counts.items(), key=lambda kv: -kv[1])[:10]))
    if not targets:
        print("ℹ 没有候选位置 —— 产物可能不是本管线产的（缺 top2/alt_candidates 字段）")
        return 0
    if args.dry_run:
        for t in targets[:20]:
            print(f"  {t['page']} L{t['line_id']} pos{t['char_index']} "
                  f"{t['c1']}→{t['c2']} r={t['ratio']} [{t['kind']}]")
        print(f"  … 共 {len(targets)} 个")
        return 0

    t0 = time.perf_counter()
    engine = C.build_engine(
        backend=args.backend, rec_only=True,
        extra_params={"Global.use_preprocess_img": False,
                      "Global.return_word_box": True,
                      "Global.return_single_char_box": True},
    )
    print(f"引擎就绪 {time.perf_counter() - t0:.2f}s")

    # 逐行跑一次二次后验（一行多个候选位共用）
    lines_needed: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for t in targets:
        lines_needed.setdefault((t["page"], t["line_id"]), t)
    print(f"需要重识别的行 {len(lines_needed)}（候选位 {len(targets)}）")

    img_cache: Dict[str, Any] = {}
    so_cache: Dict[Tuple[str, int], Any] = {}
    n_fail = 0
    n_err = 0
    err_last = ""
    for n, (key, t) in enumerate(sorted(lines_needed.items()), 1):
        try:
            so = second_opinion(P["pages"], key[0], t["bbox"], engine, img_cache)
        except Exception as e:              # 重识别异常：**计数**后继续，绝不静默
            n_err += 1
            err_last = f"{type(e).__name__}: {e}"
            so = None
        if so is None:
            n_fail += 1
        so_cache[key] = so
        if n % 50 == 0:
            print(f"  … {n}/{len(lines_needed)}")

    results: List[Dict[str, Any]] = []
    ink_hist: List[float] = []
    ink_src: Dict[str, int] = {}
    for t in targets:
        key = (t["page"], t["line_id"])
        so = so_cache.get(key)
        ink_rel = None
        src_now: Optional[str] = None
        # 墨量：字位区间平移到行裁片坐标后，在**灰度行裁片**上量。
        # ⚠️ 二次后验与产物**长度不同**时（删字/长度变了），`cols` 对的是**新文本**，
        #    目标下标在它里面根本不存在 —— 这时只能退回均分区间来量（并记下 `ink_src`）。
        if so and so.get("cols"):
            n_old = len(t["text"])
            if len(so["cols"]) == n_old:
                spans, src_now = C.char_x_spans_from_cols(t["bbox"], so["cols"]), "cols"
            else:
                spans, src_now = C.char_x_spans_even(t["bbox"], n_old), "even(长度不一致)"
            if spans and len(spans) == n_old:
                shifted = [(a - so["x0"], b - so["x0"]) for a, b in spans]
                inks = line_char_inks(so["gray"], shifted)
                have = [v for v in inks if v is not None]
                ci = int(t["char_index"])
                if have and 0 <= ci < len(inks) and inks[ci] is not None:
                    med = float(np.median(have)) or 1.0
                    ink_rel = inks[ci] / med
                    ink_hist.append(round(ink_rel, 3))
                    C.inc(ink_src, src_now)
        r = dict(t)
        if so is None:
            r.update(verdict="证据不足·待核", note="原图裁片失败", new=None, diff=None)
        else:
            r.update(judge(t, so, ink_rel, args.ink_cut))
            r["conf"] = so.get("conf")
        r["ink_rel"] = None if ink_rel is None else round(ink_rel, 3)
        r["ink_src"] = src_now
        r["adopt"], r["adopt_why"] = should_adopt(r["verdict"], r["kind"], args.adopt)
        r["source"] = "recheck"
        results.append(r)

    # ------------------------------------------------------------------
    # 合并「top2 规则 + 冻结台账」的已验证改动（--review）。
    # 🔴 没有这一步，落盘会**丢掉 13 条台账内错误**：它们的特征是"两路一致"
    #    （原图重识别读出的也是同一个错字，如 `抢/抡` 组），recheck 自己永远不会
    #    提议改它们 —— 但 top2 单向规则 + 冻结台账（34/34）已经验证过该改。
    #    ⇒ recheck 在这里的角色是**字级纠错的统一落盘器**，不是唯一证据源。
    # 冲突守卫：同一位置两源给的"正确字"不同 ⇒ 响亮报出并**跳过**（不猜谁对）。
    # ------------------------------------------------------------------
    n_review = 0
    if args.review:
        rev = C.load_json(args.review) or {}
        known = {(r["page"], r["line_id"], r["char_index"]): r for r in results}
        for it in rev.get("items") or []:
            if not it.get("known_id"):
                continue                      # 台账外条目必须走 recheck 证据，不直接采纳
            k = (it["page"], it["line_id"], it["char_index"])
            hit = known.get(k)
            if hit is not None:
                # 只有 recheck **也提议改** 且方向不同才算冲突；"两路一致·不改" /
                # "证据不足"都不是提议 —— 那正是台账内"两路一致地错"的 13 条，
                # 台账是它们唯一的证据，必须照常并入。
                if hit["verdict"] == "改字":
                    if hit.get("new") != it.get("new"):
                        print(f"⚠ 两源方向冲突 {it['page']} L{it['line_id']} "
                              f"pos{it['char_index']}：台账说→{it.get('new')!r}，"
                              f"二次后验说→{hit.get('new')!r}，跳过待人工")
                        continue
                    hit["adopt_why"] += "｜台账内（top2 规则与二次后验同向）"
                    continue
            results.append({
                "page": it["page"], "line_id": it["line_id"],
                "char_index": it["char_index"], "kind": "cjk",
                "c1": it.get("before"), "c2": None, "ratio": it.get("ratio"),
                "bbox": it.get("bbox"), "text": it.get("context") or "",
                "verdict": "改字", "new": it.get("new"), "diff": None,
                "ink_rel": None, "ink_src": None, "conf": None,
                "note": "top2 单向规则 + 冻结台账（两路一致地错，二次后验看不见）",
                "adopt": True, "source": "top2_rule",
                "adopt_why": "top2 规则 + 冻结台账 34/34（原图二次后验同错，故无第二路）",
            })
            known[k] = results[-1]
            n_review += 1
        C.inc(stat, "来自 --review 的台账条目", n_review)
        print(f"合并台账改动 {n_review} 条（两源同向 {len(rev.get('items') or [])} - 新增 {n_review}）")

    # ------------------------------------------------------------------
    # 人工复核白名单：判定 ≠ 采纳的**最后一道闸**。
    # 文件格式：[{"page","line_id","char_index"}, ...] —— 只有列在内的采纳项才落盘。
    # 不给 = 全部采纳项落盘。
    # ------------------------------------------------------------------
    if args.only_file:
        allow = {(x["page"], int(x["line_id"]), int(x["char_index"]))
                 for x in (C.load_json(args.only_file) or [])}
        if not allow:
            print(f"✗ --only-file 里没有可用条目：{args.only_file}")
            return 2
        for r in results:
            k = (r["page"], int(r["line_id"]), int(r["char_index"]))
            if r["adopt"] and k not in allow:
                r["adopt"] = False
                r["adopt_why"] += "｜人工复核未通过/待定，不落盘"

    n_adopt = sum(1 for r in results if r["adopt"])
    cnt: Dict[str, int] = {}
    for r in results:
        cnt[r["verdict"]] = cnt.get(r["verdict"], 0) + 1
    by_kind: Dict[str, Dict[str, int]] = {}
    ink_by_kind: Dict[str, List[float]] = {}
    for r in results:
        d = by_kind.setdefault(r["kind"], {})
        d[r["verdict"]] = d.get(r["verdict"], 0) + 1
        if r["ink_rel"] is not None:
            ink_by_kind.setdefault(r["kind"], []).append(r["ink_rel"])
    ink_q = {
        k: [round(float(x), 3) for x in np.quantile(np.array(v), [0.05, 0.25, 0.5, 0.75, 0.95])]
        for k, v in ink_by_kind.items() if v
    }

    P["out"].mkdir(parents=True, exist_ok=True)
    report = {
        "pages": len(pages), "targets": len(targets), "lines": len(lines_needed),
        "targets_kinds": kinds, "ratio_min": args.ratio_min,
        "adopt_policy": args.adopt, "adopted": n_adopt,
        "ink_cut": args.ink_cut, "verdicts": cnt, "by_kind": by_kind,
        "ink_quantiles_by_kind": ink_q, "ink_src": ink_src,
        "pair_counts": dict(sorted(pair_counts.items(), key=lambda kv: -kv[1])),
        "line_rec_fail": n_fail, "rec_errors": n_err,
        "rec_last_error": err_last or None, "collect": stat,
        "ink_hist": sorted(ink_hist)[:60] + sorted(ink_hist)[-20:] if ink_hist else [],
        "provenance": {"engine": C.engine_fingerprint(engine),
                       "pad": 6, "product_schema": C.PRODUCT_SCHEMA},
        "items": results,
    }
    C.dump_json(P["report_json"], report)
    C.dump_json(P["corrections"],
                {"policy": args.adopt, "n": n_adopt,
                 "note": "只含**采纳**的条目；未采纳的见 report.json 的 items[*].adopt_why",
                 "items": [r for r in results if r["adopt"]]})
    lines_md = [f"# 原图二次后验（{len(targets)} 个候选位）", "",
                f"- 采纳策略 `--adopt {args.adopt}` ⇒ **采纳 {n_adopt} 条**",
                f"- 需要重识别的行 **{len(lines_needed)}**，裁片失败 {n_fail}",
                f"- 墨量比阈值 `{args.ink_cut}`（相对本行中位字位墨量）", ""]
    for k in sorted(cnt, key=lambda x: -cnt[x]):
        lines_md.append(f"- **{k}**：{cnt[k]}")
    lines_md += ["", "| # | 采纳 | 页 | 行 | 位 | 类别 | 产物 | 第二候选 | 比值 | 二次后验 | 墨量比 | 判定 |",
                 "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for n, r in enumerate(results, 1):
        lines_md.append(
            f"| {n} | {'✅' if r['adopt'] else '—'} | {r['page']} | {r['line_id']} | "
            f"{r['char_index']} | {r['kind']} | "
            f"{r['c1']} | {r['c2']} | {r['ratio']} | {r.get('new') or '—'} | "
            f"{r.get('ink_rel') if r.get('ink_rel') is not None else '—'} | {r['verdict']} |")
    C.atomic_write_text(P["report_md"], "\n".join(lines_md) + "\n")

    print(f"\n=== 判定汇总（{len(results)} 个；采纳策略 --adopt {args.adopt}）===")
    for k in sorted(cnt, key=lambda x: -cnt[x]):
        print(f"  {k:<16} {cnt[k]:>4}")
    print(f"  ⇒ **采纳 {n_adopt} 条**（其余按「提升小的不用 / 对原文内容有损害的不用」剔除）")
    print(f"\n按类别：")
    for k, d in by_kind.items():
        print(f"  [{k}] " + "  ".join(f"{a}={b}" for a, b in sorted(d.items())))
    if ink_hist:
        q = np.quantile(np.array(ink_hist), [0.05, 0.25, 0.5, 0.75, 0.95])
        print(f"\n墨量比分布（n={len(ink_hist)}，本行中位字位=1.0）"
              f"  p5={q[0]:.2f} p25={q[1]:.2f} p50={q[2]:.2f} p75={q[3]:.2f} p95={q[4]:.2f}")
        print(f"  低于阈值 {args.ink_cut} 的：{sum(1 for v in ink_hist if v < args.ink_cut)} 个"
              f"（阈值该由这张分布定，别照搬默认值）")
        for k, v in sorted(ink_q.items()):
            print(f"  [{k}] 墨量比 p5={v[0]} p25={v[1]} p50={v[2]} p75={v[3]} p95={v[4]}")
        print(f"  墨量来源：{ink_src}")
    if n_fail:
        print(f"⚠ {n_fail} 行的原图裁片失败 —— 这些行**没被复核**，别当成「已查过」。")
    if n_err:
        print(f"🔴 重识别抛异常 {n_err} 次（最后一次：{err_last}）—— "
              f"这些行同样没被复核，且说明埋点/引擎有问题，先修它。")
    print(f"报告 -> {P['report_json']}")
    print(f"      -> {P['report_md']}")

    if args.apply:
        if not n_adopt:
            print("ℹ 采纳 0 条（当前策略下没有可写的改动）—— 产物未动")
            return 0
        n_pages = apply_corrections(P["products"], P["corrected"], results)
        print(f"已写出修正产物 {n_pages} 页（{n_adopt} 处）-> {P['corrected']}")
    else:
        print("（未加 --apply：只出报告，产物未改动）")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
