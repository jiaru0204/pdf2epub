# -*- coding: utf-8 -*-
"""linefix.audit —— 漏行审计：det 框与既有产物做空间集合差（**零推理**）。

取框只有一条路径：**产物的 `det_boxes` 字段**
----------------------------------------------
产线的 OCR 主前向本来就跑过 det，框已经算出来了 —— 让它顺手落盘到产物即可：

    {"page": "...", "line_count": N, "lines": [...],
     "det_boxes": [[x, y, w, h], ...]}      # 原图坐标，整数

对账时直接读，**一次推理都不做**。这与现场再跑一遍 det 逐框等价，不是近似。

🔴 `det_boxes` 必须是**过滤前**的原始框
    这字段极易做错：rapidocr 的 `RapidOCR.__call__` 返回前有**两道过滤**，
    而且两道都是「框与文本一起删」（`main.py:183` 空文本、`main.py:349` 分数）。
    于是 `result.boxes` 与 `result.txts` **永远 1:1** —— 拿它落盘，
    本模块的差异恒为 0，一个漏行都查不出来（看着"全绿"，其实失明）。
    生产者（`pipeline.ocr`）通过挂钩 `rapidocr.main.map_boxes_to_original`
    取过滤前的框；若它降级了，会在 `provenance.det_boxes_source` 留 `filtered`，
    本模块见到会**响亮警告**并把页列出来。

❌ **没有 det-only 回退，是刻意的**
   保留回退 = 一条"看着能用、其实一直在兜底"的隐路径，它会掩盖真正的缺陷：
   **生产者压根没把 det 框落盘**。而回退要付 ~1.7~1.9 s/页 × 全书，
   却产不出一条新信息（框与主前向逐框相同）。
   → 缺字段就**响亮跳过**并计入 `no_det_boxes`，让生产者去修。
   **响亮失败优于静默降级。**

⚠️ 推论：本模块**不依赖 rapidocr**，纯 CPU + 读 JSON，单页毫秒级，
   不需要缓存、不需要 `--force`、不需要指纹校验。

本项目的产物口径（`PRODUCT_SCHEMA` 6）怎么对上
----------------------------------------------
- `lines[].role`：`"text"` / `"page_number"`。页码行**只打标记、不删除**，
  所以它照样出现在 `lines` 里、照样和 det 框一一对上 —— 本模块因此**不需要**为
  页码做任何特殊处理。⚠️ 反过来说：**千万不能**在下游把页码行删掉再喂进来，
  否则页码框会被误报成"有框无行"（这正是当初改成"只标记不删除"的原因）。
- `dropped_lines[].det_index`：OCR 主前向自己声明的丢弃行。它是**另一条独立证据**，
  本模块会拿它和 `diff_boxes` 的结论做交叉核对（`dropped_evidence()`），
  对不上就响亮报出 —— 两条路径同源但实现不同，对不上必有一侧口径坏了。
- `provenance.audit_capable`：`False` = 产物来自降级路径，下游**必须拒绝审计**。

判定口径
--------
- 匹配：det 框中心落在某产物行的 bbox（外扩 `--contain-pad`）内，**或** IoU ≥ `--iou-min`。
- 未匹配的 det 框 = 差异框，再经最小尺寸过滤（**按页尺寸归一化**，换书不必手调）。

两个方向的含义（本工具只覆盖前者）
----------------------------------
    det 有框 / 产物无行  → rec 失败 或 conf 被 text_score 静默丢弃   ← 本工具的报告
    det 无框 / 产物无行  → det 自身漏检（需别的依据，本工具看不见）

用法
----
    python -m linefix.audit --pages <页目录> --products <产物目录> [--out <输出目录>]
    python -m linefix.audit --pages pages --products textlines --pages-filter page_099
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from . import common as C

DET_SOURCE = "product.det_boxes"


def _paths(args) -> Dict[str, Path]:
    pages = Path(args.pages)
    out = Path(args.out) if args.out else pages.parent / "linefix"
    return {
        "pages": pages,
        "products": Path(args.products),
        "out": out,
        "detrev": out / "audit" / "detrev",
        "summary": out / "audit" / "summary.json",
    }


def boxes_from_product(prod: Dict[str, Any]):
    """读产物的 `det_boxes` 字段，重建四点框。

    返回 `(boxes, bad_count)`：
      - `boxes is None` → 字段**缺失**（生产者没落盘 → 调用方应响亮跳过）
      - `boxes` 为空数组 → 字段存在但为空（该页确实没有 det 框，如空白页）
      - `bad_count` → 格式不合法而被丢弃的条目数
    """
    stored = prod.get("det_boxes") if isinstance(prod, dict) else None
    if stored is None:
        return None, 0
    if not stored:
        return np.zeros((0, 4, 2), dtype=float), 0
    quad, bad = [], 0
    for item in stored:
        try:
            x, y, w, h = (float(v) for v in item)
        except Exception:
            bad += 1
            continue
        quad.append([[x, y], [x + w, y], [x + w, y + h], [x, y + h]])
    if not quad:
        return np.zeros((0, 4, 2), dtype=float), bad
    return np.asarray(quad, dtype=float), bad


def filtered_det_source(prod: Dict[str, Any]) -> bool:
    """产物是否来自**降级**的取框路径（用了过滤后的 det 框）。

    rapidocr 的 `RapidOCR.__call__` 返回前有**两道过滤**，且都是「框与文本一起删」
    （`main.py:183` 空文本、`main.py:349` 分数）。所以过滤后的 `boxes` 与 `txts`
    **永远 1:1** —— 拿它当 `det_boxes`，本模块的差异恒为 0，一个漏行都查不出来。
    生产者若走了这条降级路径，会在 `provenance.det_boxes_source` 留 `filtered` 标记。
    """
    prov = prod.get("provenance") if isinstance(prod, dict) else None
    return isinstance(prov, dict) and prov.get("det_boxes_source") == "filtered"


def dropped_evidence(prod: Dict[str, Any]) -> Optional[List[int]]:
    """产物**自己声明**的丢弃行所对应的 det 下标（`dropped_lines[].det_index`）。

    返回 `None` 表示本次没采集（`--no-alts`，字段是 `null`），`[]` 表示采集了、
    确实一条都没丢 —— **两者语义不同，不能混**（见 `pipeline/ocr.py` 的口径）。

    用途：与本模块算出来的差异做**交叉核对**。`dropped_lines` 是 OCR 主前向
    "框在、文本被过滤掉"的现场证据；`diff_boxes` 是"框在、产物里没有这一行"的
    空间集合差。两条路径同源但实现不同，**它们对不上就说明有一侧口径坏了** ——
    这正是本项目最容易被静默吞掉的那类缺陷。
    """
    if not isinstance(prod, dict):
        return None
    raw = prod.get("dropped_lines")
    if raw is None:
        return None
    out: List[int] = []
    for d in raw:
        if isinstance(d, dict) and d.get("det_index") is not None:
            out.append(int(d["det_index"]))
    return out


def diff_boxes(
    det_boxes: np.ndarray, prod_boxes: List[tuple], min_w: int, min_h: int,
    iou_min: float, contain_pad: float,
) -> List[Dict[str, Any]]:
    """det 框里找不出对应产物行的那些（即漏行候选）。"""
    diffs: List[Dict[str, Any]] = []
    for bi, db in enumerate(det_boxes):
        d = C.xywh(db)
        if any(C.iou(d, pb) >= iou_min or C.center_in(d, pb, contain_pad)
               for pb in prod_boxes):
            continue
        if d[2] < min_w or d[3] < min_h:
            continue        # 太小 = 噪声/标点碎片，不是"整行漏"
        diffs.append({
            "det_index": int(bi),
            "bbox": [int(round(v)) for v in d],
            "reason": "det_box_no_product_line",
        })
    return diffs


def run(args) -> int:
    P = _paths(args)
    P["detrev"].mkdir(parents=True, exist_ok=True)

    pages = C.list_pages(P["pages"], args.pattern)
    if not pages:
        print(f"✗ 在 {P['pages']} 里没找到匹配 {args.pattern} 的页面")
        return 2
    if args.pages_filter:
        want = set(args.pages_filter)
        pages = [p for p in pages if p in want]
        missing = want - set(pages)
        if missing:
            print(f"⚠ 指定的页不在页面目录里：{sorted(missing)}")
    if args.limit:
        pages = pages[: args.limit]

    print(f"页面 : {P['pages']}  （{len(pages)} 页）")
    print(f"产物 : {P['products']}")
    print(f"输出 : {P['out']}")
    print(f"取框 : 产物 {DET_SOURCE} 字段（零推理）｜跳过图版页: {not args.no_skip_fig}")

    if args.dry_run:
        ok = miss = 0
        for p in pages:
            prod = C.read_lines(P["products"] / f"{p}.textlines.json")
            if prod is None:
                tag = "产物 ✗"
            elif prod.get("det_boxes") is None:
                tag = "产物 ✓  但缺 det_boxes 字段（将被跳过）"
                miss += 1
            else:
                tag = f"产物 ✓  det_boxes={len(prod['det_boxes'])}"
                ok += 1
            if prod is not None and filtered_det_source(prod):
                tag += "  ⚠ det_boxes 是过滤后的框 → 本页对账无效"
            print(f"  {p}: {tag}")
        print(f"\n可对账 {ok} 页｜缺 det_boxes 将被跳过 {miss} 页")
        return 0

    fig_cache: Dict[str, bool] = {}
    records: List[Dict[str, Any]] = []
    diffs_all: List[Dict[str, Any]] = []
    n_fig = n_noprod = n_nodet = n_bad = n_filtered = 0
    n_fig_unknown = 0
    n_drop_declared = 0          # 产物自己声明的丢弃行（`dropped_lines`）
    n_drop_tiny = 0              # 其中因太小被尺寸门槛挡掉的（碎片，本就该忽略）
    n_drop_unexplained: List[str] = []   # 声明了丢弃行，但 audit 没算出对应差异
    n_drop_no_evidence: List[str] = []   # audit 算出差异，但产物没声明（`--no-alts` 属正常）
    filtered_pages: List[str] = []
    fig_pages: List[str] = []
    t0 = time.time()

    for i, page in enumerate(pages, 1):
        png = P["pages"] / f"{page}.png"
        if not png.exists():
            print(f"[{i}/{len(pages)}] {page}: 缺页面图，跳过")
            continue

        prod = C.read_lines(P["products"] / f"{page}.textlines.json")
        if prod is None:
            n_noprod += 1
            records.append({"page": page, "skipped": "no_product"})
            print(f"[{i}/{len(pages)}] {page}: 无产物行文件，跳过")
            continue

        # 生产者若降级用了过滤后的 det 框，本页对账恒为 0 差异 —— 必须响亮标出，
        # 否则「没报漏行」会被误读成「没有漏行」。
        if filtered_det_source(prod):
            n_filtered += 1
            filtered_pages.append(page)

        det_boxes, n_bad_here = boxes_from_product(prod)
        if det_boxes is None:
            n_nodet += 1
            records.append({"page": page, "skipped": "no_det_boxes_field"})
            print(f"[{i}/{len(pages)}] {page}: ✗ 产物缺 det_boxes 字段，跳过"
                  f"（生产者未落盘 det 框 —— 请修生产者，不要在这里兜底）")
            continue
        n_bad += n_bad_here

        is_fig = C.is_figure_page(png, args.fig_ink, fig_cache)
        if is_fig is None:
            # `None` = 判不了（不是"确定不是图版页"）。审计侧的选择是**照常审计**：
            # 少审一页 = 漏行静默漏掉，多审一页只是多跑几毫秒。
            n_fig_unknown += 1
        if is_fig and not args.no_skip_fig:
            n_fig += 1
            fig_pages.append(page)
            rec = {"page": page, "engine": "linefix.audit", "det_source": DET_SOURCE,
                   "det_boxes": int(len(det_boxes)), "prod_lines": len(prod["lines"]),
                   "diff_count": 0, "figure_page": True, "diffs": []}
            C.dump_json(P["detrev"] / f"{page}.detrev.json", rec)
            records.append(rec)
            print(f"[{i}/{len(pages)}] {page}: 图版页，跳过（艺术字 rec 失败非漏行）")
            continue

        px = C.page_size(png) or (0, 0)
        min_w, min_h = C.min_box_thresholds(px[0], px[1], args.min_w_ratio, args.min_h_ratio)
        prod_boxes = [tuple(float(v) for v in l["bbox"]) for l in prod["lines"]]
        diffs = diff_boxes(det_boxes, prod_boxes, min_w, min_h, args.iou_min, args.contain_pad)

        # ---- 交叉核对：产物声明的丢弃行 vs 本模块算出的差异 ----
        # 两条独立路径：① OCR 主前向里"框在、文本被过滤"，② 本模块的 det−lines 集合差。
        # 前者若有条目在后者里找不到，说明**本模块的匹配口径**放过了它（IoU/中心包含/尺寸门槛），
        # 或者产物被改过却没更新 `dropped_lines`（它是历史快照，不会被回头改写）。
        #
        # 🔴 **只看过了尺寸门槛的丢弃行**：`dropped_lines` 里含大量 22×20、9×16 这种
        # 标点/噪点碎片（实测本书 2 条全是这种），它们被 `diff_boxes` 的 `min_w/min_h`
        # 刻意忽略 —— 那是"太小不是整行漏"，不是口径不符。拿全部丢弃行去对账会
        # **假报警**（本节第一版就是这么错的，全书 2 页全被误报）。
        declared = dropped_evidence(prod)
        dropped_unexplained: List[int] = []
        if declared is not None:
            n_drop_declared += len(declared)
            diff_idx = {d["det_index"] for d in diffs}
            for k in declared:
                if not (0 <= k < len(det_boxes)):
                    dropped_unexplained.append(k)      # 下标越界本身就是坏产物
                    continue
                dd = C.xywh(det_boxes[k])
                if dd[2] < min_w or dd[3] < min_h:
                    n_drop_tiny += 1                   # 碎片：本就该被门槛挡掉
                    continue
                if k not in diff_idx:
                    dropped_unexplained.append(k)
            if dropped_unexplained:
                n_drop_unexplained.append(page)
            elif declared == [] and diffs:
                # 采集了、明确说"一条没丢"，但 audit 算出了差异 → 必有一侧不对。
                n_drop_no_evidence.append(page)

        rec = {
            "page": page, "engine": "linefix.audit", "det_source": DET_SOURCE,
            "det_boxes": int(len(det_boxes)), "prod_lines": int(len(prod["lines"])),
            "diff_count": len(diffs), "figure_page": False,
            "page_size": [px[0], px[1]], "min_box": [min_w, min_h],
            "page_number_lines": sum(1 for l in prod["lines"]
                                     if l.get("role") == "page_number"),
            "dropped_declared": declared,
            "dropped_unexplained": dropped_unexplained,
            "diffs": diffs,
        }
        C.dump_json(P["detrev"] / f"{page}.detrev.json", rec)
        records.append(rec)
        if diffs:
            diffs_all.extend({"page": page, **d} for d in diffs)
        print(f"[{i}/{len(pages)}] {page}: det={rec['det_boxes']} prod={rec['prod_lines']} "
              f"差异={len(diffs)}{'  ← 漏行候选' if diffs else ''}")

    tot = {
        "pages": len(records),
        "det_boxes": sum(r.get("det_boxes", 0) for r in records),
        "prod_lines": sum(r.get("prod_lines", 0) for r in records),
        "diffs": len(diffs_all),
        "figure_skipped": n_fig,
        "no_product": n_noprod,
        "no_det_boxes": n_nodet,
        "malformed_det_boxes": n_bad,
        "filtered_det_boxes": n_filtered,
        "filtered_pages": filtered_pages,
        "figure_unknown": n_fig_unknown,
        "dropped_declared": n_drop_declared,
        "dropped_below_min_size": n_drop_tiny,
        "dropped_unexplained_pages": n_drop_unexplained,
        "dropped_no_evidence_pages": n_drop_no_evidence,
        "elapsed_sec": round(time.time() - t0, 2),
    }
    C.dump_json(P["summary"], {
        "audit_dir": str(P["out"] / "audit"),
        "pages_dir": str(P["pages"]),
        "products_dir": str(P["products"]),
        "det_source": DET_SOURCE,
        "thresholds": {
            "iou_min": args.iou_min, "contain_pad": args.contain_pad,
            "min_w_ratio": args.min_w_ratio, "min_h_ratio": args.min_h_ratio,
            "fig_ink": args.fig_ink,
        },
        "totals": tot,
        "pages": records,
        "diffs": diffs_all,
    })

    print(f"\nDONE {tot['pages']} 页对账  det={tot['det_boxes']} prod={tot['prod_lines']} "
          f"差异={tot['diffs']}")
    print(f"  图版页跳过 {n_fig}｜无产物 {n_noprod}｜**缺 det_boxes 跳过 {n_nodet}**"
          f"｜格式非法框 {n_bad}｜耗时 {tot['elapsed_sec']}s")
    print(f"  交叉核对：产物声明丢弃行 {n_drop_declared} 条"
          f"（其中 {n_drop_tiny} 条低于最小尺寸门槛、按碎片忽略）"
          f"｜audit 未算出差异 {len(n_drop_unexplained)} 页"
          f"｜声明无丢弃却有差异 {len(n_drop_no_evidence)} 页")
    print(f"  汇总 -> {P['summary']}")
    if n_fig_unknown:
        print(f"\nℹ {n_fig_unknown} 页的图版判据返回 None（判不了，照常审计）"
              f"—— `is_figure_page` 的 None 不等于「不是图版页」，见 common 的说明。")
    if n_fig:
        # 报出**具体哪些页**被跳过：这条启发式（暗像素占比）会连"黑底白字的正文页"
        # 一起跳过（实测某书封面 98% 暗却含 26 行可识别正文）→ 覆盖面上有个隐形缺口，
        # 不看页名就发现不了。要审计它们加 `--no-skip-fig`。
        print(f"\nℹ {n_fig} 页被判为图版页而**跳过**（暗像素占比 > {args.fig_ink}）："
              f"{', '.join(fig_pages[:10])}{' …' if len(fig_pages) > 10 else ''}")
        print(f"  这层启发式会连「黑底白字的正文页」一起跳过 —— 如确为正文，"
              f"用 `--no-skip-fig` 重跑这几页。")
    if n_filtered:
        print(f"\n🔴 {n_filtered} 页的 det_boxes 是**过滤后**的框（`provenance.det_boxes_source"
              f" = filtered`）—— 这些页的差异恒为 0，对账**无效**。")
        print(f"  原因：OCR 主链路必须落盘**过滤前**的原始 det 框，否则一个漏行都查不出来。")
        print(f"  页：{', '.join(filtered_pages[:10])}{' …' if len(filtered_pages) > 10 else ''}")
    if n_nodet:
        print(f"\n⚠ {n_nodet} 页因产物缺 det_boxes 字段被跳过 —— 这是**生产者的缺口**：")
        print(f"  产线 OCR 写产物时应带上该字段（格式 [[x, y, w, h], ...]，原图坐标）。")
        print(f"  本模块刻意不做 det 回退：静默兜底会掩盖生产者没接上这件事。")
    if n_drop_unexplained:
        # 声明了丢弃行、audit 却算不出对应差异 ⇒ 有一侧口径不对，**不能当全绿**。
        print(f"\n🔴 {len(n_drop_unexplained)} 页存在「产物声明丢弃、audit 未算出差异」的"
              f"不符（声明 {n_drop_declared} 条）："
              f"{', '.join(n_drop_unexplained[:10])}{' …' if len(n_drop_unexplained) > 10 else ''}")
        print(f"  两条独立路径对不上 —— 要么匹配口径（IoU/中心包含/最小尺寸）放过了它，")
        print(f"  要么产物被改过而 `dropped_lines` 仍是历史快照。先查清再读这张表。")
    if n_drop_no_evidence:
        print(f"\n🔴 {len(n_drop_no_evidence)} 页「声明没有丢弃行，audit 却算出差异」："
              f"{', '.join(n_drop_no_evidence[:10])}{' …' if len(n_drop_no_evidence) > 10 else ''}")
        print(f"  这几乎一定意味着产物**不是这条产线产的**（或采集口径变了）。")
    if diffs_all:
        print("\n差异清单（= 漏行候选，交给 linefix.rescue）：")
        for d in diffs_all:
            print(f"  {d['page']}  bbox={d['bbox']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m linefix.audit",
        description="漏行审计：读产物的 det_boxes 字段与产物行做空间集合差（零推理）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--pages", required=True, help="页面 PNG 目录")
    ap.add_argument("--products", required=True,
                    help="文本行产物目录（*.textlines.json，须含 det_boxes 字段）")
    ap.add_argument("--out", default=None, help="输出目录（默认 <pages>/../linefix）")
    ap.add_argument("--pattern", default="page_*.png", help="页面文件通配（默认 page_*.png）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 页（抽样试跑）")
    ap.add_argument("--pages-filter", nargs="*", default=None,
                    help="只处理指定页（如 page_099 page_170）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只列页与产物状态（含哪些页缺 det_boxes），不写产物")
    ap.add_argument("--no-skip-fig", dest="no_skip_fig", action="store_true",
                    help="不跳过图版页")
    ap.add_argument("--fig-ink", type=float, default=C.FIG_INK, help="图版页暗像素占比阈值")
    ap.add_argument("--iou-min", type=float, default=0.30, help="det 框与产物行的最小 IoU")
    ap.add_argument("--contain-pad", type=float, default=8, help="中心包含判定外扩像素")
    ap.add_argument("--min-w-ratio", type=float, default=C.MIN_BOX_W_RATIO,
                    help="差异框最小宽度 = 该比例 × 页宽")
    ap.add_argument("--min-h-ratio", type=float, default=C.MIN_BOX_H_RATIO,
                    help="差异框最小高度 = 该比例 × 页高")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    C.force_utf8_stdout()
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
