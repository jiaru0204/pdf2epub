# -*- coding: utf-8 -*-
"""linefix.rescue —— 漏行救援：从**原图**裁精确框做 rec-only 重识别。

为什么这样能修好
----------------
主链路是在 L1 降采样后的整页图上裁行的（长边被压到 1984，行高只剩 36~50px），
小行高会 rec 退化成幻觉（conf 0.4~0.5）→ 再被 `text_score=0.5` **静默丢弃**。
本模块绕开这条路径：**从原图裁框 → 单行 rec-only → 验收 → 写修正产物**。

三类目标
--------
| 目标 | 来源 | 裁剪对象 |
|------|------|---------|
| W1 漏行 | `linefix.audit` summary.json 的差异框 | det 精确 bbox |
| W2 低置信度行 | 产物中 conf < `--conf-w2` 的行 | 原行 bbox |

⚠️ 与旧实现的差异（本移植版新修）
1. **`orig_line_id` / `added` 落进产物** —— 重排 line_id 后仍能确定性回溯到源行号，
   下游不必再用"最大引用行号容纳性"这类启发式去猜 AI 看到的是哪个行号空间。
2. **图版页过滤真正生效** —— 旧版定义了 `is_figure_page` 却从未调用，
   docstring 承诺的"W2 只治文字页"没实现，导致艺术字低 conf 行被当目标重识别。
3. **`corrections.json` 的 key 改为 `页码:行号`** —— 旧版用"旧文本"当 key，
   跨页同文本会互相覆盖，审计链静默丢条目。
4. **排序对齐引擎口径** —— 与 `_BOX_SORT_Y_THRESHOLD=10` 一致，而非精确 (y,x)。

用法
----
    python -m linefix.rescue --pages <页目录> --products <产物目录> \\
                             [--audit <audit 输出目录>] [--out <输出目录>] [--dry-run]
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import common as C


def _paths(args) -> Dict[str, Path]:
    pages = Path(args.pages)
    root = Path(args.out) if args.out else pages.parent / "linefix"
    audit = Path(args.audit) if args.audit else root / "audit"
    rescue = root / "rescue"
    return {
        "pages": pages,
        "products": Path(args.products),
        "audit": audit,
        "audit_summary": audit / "summary.json",
        "root": root,
        "rescue": rescue,
        "corrected": rescue / "corrected",
        "evidence": rescue / "evidence",
        "corrections": rescue / "corrections.json",
        "report": rescue / "report.md",
    }


def crop(page_png: Path, bbox, pad: int):
    """按 bbox 从原图裁框。读出即 BGR，直接传 ndarray（引擎不再转通道）。"""
    img = C.imread_u(page_png)
    if img is None:
        return None
    H, W = img.shape[:2]
    x, y, w, h = [int(v) for v in bbox]
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(W, x + w + pad), min(H, y + h + pad)
    if x1 <= x0 or y1 <= y0:
        return None
    return img[y0:y1, x0:x1]


def rec_only(engine, im) -> Tuple[str, float]:
    """单行 rec-only 识别，返回 (text, conf)。多行时取最长的那条。"""
    res = engine(im)
    if res is None or not getattr(res, "txts", None):
        return "", 0.0
    return max(zip(res.txts, res.scores), key=lambda p: len(p[0]))[:2]


def collect_targets(args, P: Dict[str, Path], fig_cache: Dict[str, bool]):
    """汇总 W1（来自 audit）与 W2（产物低 conf），返回 {page: {"w1": [...], "w2": [...]}}。"""
    tg: Dict[str, Dict[str, list]] = {}
    page_filter = set(args.pages_filter or [])

    summary = C.load_json(P["audit_summary"])
    if summary is None:
        print(f"⚠ 未找到 {P['audit_summary']}（W1 部分为空）—— 请先跑 python -m linefix.audit")
    else:
        for d in summary.get("diffs", []):
            if page_filter and d["page"] not in page_filter:
                continue
            # ⚠️ 连 `det_index` 一起带走（参考版只带了 bbox）：补出来的行必须**认领**
            # 那个原来没有行的 det 框，否则修补后产物的
            # `lines ∪ dropped 的 det_index 恰好划分全部 det_boxes` 不变式就断了 ——
            # 下游再也无法把这一行挂回它的证据框。
            tg.setdefault(d["page"], {"w1": [], "w2": []})["w1"].append(
                {"bbox": d["bbox"], "det_index": d.get("det_index")})

    n_fig_skipped = 0
    for p in sorted(P["products"].glob("*.textlines.json")):
        doc = C.read_lines(p)
        if doc is None:
            continue
        page = doc.get("page") or p.stem.replace(".textlines", "")
        if page_filter and page not in page_filter:
            continue
        low = [l for l in doc["lines"]
               if l.get("confidence") is not None
               and l["confidence"] < args.conf_w2]

        # ✅ 修：图版页过滤真正生效（低 conf 的艺术字不是"该救的行"）
        # 注意 `is_figure_page` 返回 `Optional[bool]`：`None` = 判不了，这里按
        # **非图版页**处理（宁可多救一条，也不要因为"判不了"把正文行静默丢掉）。
        if low and C.is_figure_page(P["pages"] / f"{page}.png", args.fig_ink, fig_cache) is True:
            n_fig_skipped += len(low)
            low = []

        if low:
            tg.setdefault(page, {"w1": [], "w2": []})["w2"] = [
                {"line_id": l["line_id"], "bbox": l["bbox"],
                 "text": l["text"], "conf": l["confidence"]} for l in low]
    return tg, n_fig_skipped


def run(args) -> int:
    P = _paths(args)
    P["rescue"].mkdir(parents=True, exist_ok=True)
    if not args.dry_run:
        P["corrected"].mkdir(parents=True, exist_ok=True)
        P["evidence"].mkdir(parents=True, exist_ok=True)

    fig_cache: Dict[str, bool] = {}
    tg, n_fig_skipped = collect_targets(args, P, fig_cache)
    if n_fig_skipped:
        print(f"（图版页过滤掉 {n_fig_skipped} 条低置信度行 —— 艺术字不可救，非漏行）")
    if not tg:
        print("无 W1/W2 目标，退出。")
        return 0

    n_w1 = sum(len(v["w1"]) for v in tg.values())
    n_w2 = sum(len(v["w2"]) for v in tg.values())
    print(f"目标：W1 差异框 {n_w1} 个（{sum(1 for v in tg.values() if v['w1'])} 页）、"
          f"W2 低 conf 行 {n_w2} 条（{sum(1 for v in tg.values() if v['w2'])} 页）")

    if args.dry_run:
        print("\n[dry-run] 将处理：")
        for name, v in sorted(tg.items()):
            for w in v["w1"]:
                b = w["bbox"] if isinstance(w, dict) else w
                di = w.get("det_index") if isinstance(w, dict) else None
                print(f"  {name}  [W1] det_index={di} bbox={b}")
            for l in v["w2"]:
                print(f"  {name}  [W2] id={l['line_id']} conf={l['conf']} {l['text'][:40]!r}")
        return 0

    print("\n构造引擎（rec-only 重识别）...")
    engine = C.build_engine(backend=args.backend, rec_only=True)
    fp = C.engine_fingerprint(engine)
    print(f"  后端={fp['backend']}  rec={fp['rec']}  "
          f"（use_det/use_cls/use_vertical_padding 均已关闭）")

    corrections = C.load_json(P["corrections"], {}) or {}
    report: List[str] = []
    n_ok = n_rej = n_top2_invalidated = 0
    t0 = time.time()

    for i, (page, v) in enumerate(sorted(tg.items()), 1):
        png = P["pages"] / f"{page}.png"
        src = P["products"] / f"{page}.textlines.json"
        if not png.exists() or not src.exists():
            print(f"[{i}/{len(tg)}] {page}: 缺页面图或产物，跳过")
            continue

        prod = C.read_lines(src)
        lines: List[Any] = [dict(l) for l in prod["lines"]]
        for ln in lines:
            ln.setdefault("orig_line_id", ln.get("line_id"))
            ln.setdefault("added", False)
        additions: List[Any] = []
        # 新增行也要有 `role`（PRODUCT_SCHEMA 6 起是必备语义字段）——
        # 判据**复用 `common.page_number_role`**，不在这里另写一份"整行是数字就算页码"。
        # 传页面高度是必须的：只按文本判会把正文区的纯数字行（如 `——”` 误识成 `99`）当页码。
        _px = C.page_size(png)
        page_h = _px[1] if _px else 0

        # ---------------- W1：补漏行（新增行，无原文本可比） ----------------
        for w in v["w1"]:
            b = w["bbox"] if isinstance(w, dict) else w
            det_idx = w.get("det_index") if isinstance(w, dict) else None
            im = crop(png, b, args.pad)
            if im is None:
                continue
            txt, conf = rec_only(engine, im)
            ok, why = C.accept(txt, conf, conf_accept=args.conf_accept)
            tag = f"{page} [W1] bbox={b} -> {txt[:50]!r} conf={conf:.3f}"
            if ok:
                bbox_i = [int(x) for x in b]
                additions.append({
                    "line_id": 0, "orig_line_id": None, "added": True,
                    "bbox": bbox_i, "text": txt,
                    "confidence": round(float(conf), 3),
                    "role": C.page_number_role(txt, bbox_i, page_h),
                    # 认领 audit 报出的那个 det 框（见 collect_targets 的说明）
                    "det_index": det_idx,
                })
                report.append(f"- OK  {tag}")
                n_ok += 1
            else:
                report.append(f"- REJ {tag}  ({why})")
                n_rej += 1
            C.imwrite_u(P["evidence"] / f"{page}_w1_{int(b[0])}_{int(b[1])}.png", im)

        # ---------------- W2：重识别低 conf 行 ----------------
        for l in v["w2"]:
            lid = l["line_id"]
            im = crop(png, l["bbox"], args.pad_line)
            if im is None:
                continue
            txt, conf = rec_only(engine, im)
            ok, why = C.accept(txt, conf, old_len=len(l["text"]), conf_accept=args.conf_accept)
            tag = (f"{page} [W2] id={lid} {l['conf']:.3f} {l['text'][:32]!r} "
                   f"-> {txt[:40]!r} conf={conf:.3f}")
            if ok:
                for ln in lines:
                    if ln.get("orig_line_id") == lid or ln.get("line_id") == lid:
                        old_len = len(ln.get("text") or "")
                        # ⚠️ 必须作废旧 `top2`：它的 `i` 是**按旧文本**算出来的字符下标，
                        #    文本一换，同一个 `i` 就指向别的字 —— 留着会让下游拿着
                        #    错的下标去解释候选（静默错锚，正是本项目反复踩的那一类）。
                        #    救援只用 rec、不做概率采集，所以这里没有新 top2 可补。
                        if ln.pop("top2", None) is not None:
                            ln["top2_invalidated"] = f"text_rewritten:{old_len}->{len(txt)}"
                            n_top2_invalidated += 1
                        ln["text"] = txt
                        ln["confidence"] = round(float(conf), 3)
                        ln["rescanned"] = True
                        break
                # ✅ 修：key 用「页码:行号」，不再用易撞车的旧文本
                corrections[f"{page}:{lid}"] = {
                    "to": txt, "page": page, "line_id": lid,
                    "src": "linefix.rescue", "conf": round(float(conf), 3),
                }
                report.append(f"- OK  {tag}")
                n_ok += 1
            else:
                report.append(f"- REJ {tag}  ({why})")
                n_rej += 1
            C.imwrite_u(P["evidence"] / f"{page}_w2_id{int(lid):03d}.png", im)

        # ---------------- 合并 + 重排（引擎口径） ----------------
        touched = bool(additions) or any(ln.get("rescanned") for ln in lines)
        if not touched:
            print(f"[{i}/{len(tg)}] {page}: 无采纳项，未写出")
            continue

        if additions:
            lines.extend(additions)
        lines = C.sort_lines_like_engine(lines)
        for idx, ln in enumerate(lines, start=1):
            ln["line_id"] = idx

        # 🔴 provenance **必须合并源产物**，不能只写引擎指纹：
        #    `det_boxes_source` / `top2_floor` / `top2_decimals` / `audit_capable` /
        #    `alignment` 这些口径字段都在源 provenance 里。只写指纹的话，
        #    修补后的产物会丢掉「det 框是原始框还是过滤后的框」这个信息 ——
        #    下一次 audit 就再也发现不了降级产物（静默变绿）。
        prov = dict(prod.get("provenance") or {})
        prov["product_schema"] = C.PRODUCT_SCHEMA
        prov["rescue"] = {
            "engine": fp,
            "schema": 1,
            "added": len(additions),
            "rewritten": sum(1 for ln in lines if ln.get("rescanned")),
            "top2_invalidated": sum(1 for ln in lines if ln.get("top2_invalidated")),
            "source_product_schema": prod.get("provenance", {}).get("product_schema")
            if isinstance(prod.get("provenance"), dict) else None,
        }

        C.write_lines(
            P["corrected"] / f"{page}.textlines.json", page, lines, prov,
            orig_line_count=prod.get("line_count", len(prod["lines"])),
            added=len(additions),
            # 透传源产物的 det 框（检测没被改动）—— 否则修补后的产物缺该字段，
            # 下一次 audit 只能把它当"生产者没接上"跳过，闭环断掉。
            det_boxes=prod.get("det_boxes"),
            # 原 OCR 时的「丢弃行现场证据」也带着走。它是**历史快照**：救援会为其中
            # 一部分补出行来，但不会回头改这个字段（改了就等于篡改证据）。
            # 要看救援之后**还剩**多少有框无行，请用 `det_boxes − lines`（audit 的口径）。
            dropped_lines=prod.get("dropped_lines"),
        )
        n_rewritten = sum(1 for ln in lines if ln.get("rescanned"))
        print(f"[{i}/{len(tg)}] {page}: 原 {prod.get('line_count')} 行 -> {len(lines)} 行"
              f"（补 {len(additions)}，改写 {n_rewritten}）")

    # ---------------- 留痕：非破坏性写 corrections.json ----------------
    prev_raw = C.load_json(P["corrections"], {}) or {}
    foreign = {k: v for k, v in prev_raw.items()
               if k != "__meta__"
               and not (isinstance(v, dict) and v.get("src") == "linefix.rescue")}
    if foreign:
        bak = P["corrections"].with_suffix(
            f".json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        C.dump_json(bak, prev_raw)
        print(f"⚠ corrections.json 含 {len(foreign)} 条非本工具条目，已备份 -> {bak.name}")
        for k, vv in foreign.items():
            corrections.setdefault(k, vv)

    merged = {
        "__meta__": {
            "engine": "linefix.rescue", "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "provenance": fp, "targets": {"w1": n_w1, "w2": n_w2},
            "adopted": n_ok, "rejected": n_rej,
            "rejected_note": "拒绝项 = 验收未过，已记入 report，**未改动产物**",
        }
    }
    merged.update({k: v for k, v in corrections.items() if k != "__meta__"})
    C.dump_json(P["corrections"], merged)

    P["report"].write_text(
        f"# linefix.rescue 报告\n\n"
        f"- 后端 / 模型：{fp['backend']} / {fp['rec']}\n"
        f"- 目标：W1 差异框 {n_w1} / W2 低 conf 行 {n_w2}\n"
        f"- 结果：采纳 {n_ok}，拒绝（转人工）{n_rej}\n"
        f"- 耗时 {time.time() - t0:.0f}s\n"
        f"- 输出：`{P['corrected']}`\n"
        f"- ⚠️ 拒绝项**未写入产物**，需人工看证据图 `evidence/`\n\n"
        f"## 明细\n\n" + "\n".join(report) + "\n",
        encoding="utf-8",
    )

    print(f"\nDONE 采纳 {n_ok}，拒绝 {n_rej}，{time.time() - t0:.0f}s")
    if n_top2_invalidated:
        print(f"  已作废 {n_top2_invalidated} 行的 top2（文本被改写，下标失效）"
              f"—— 这些行保留 `top2_invalidated` 标记，形近字分析会跳过它们。")
    print(f"  修正产物 -> {P['corrected']}")
    print(f"  替换清单 -> {P['corrections']}")
    print(f"  报告     -> {P['report']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m linefix.rescue",
        description="漏行救援：原图裁精确框 + rec-only 重识别 + 验收 + 留痕",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--pages", required=True, help="页面 PNG 目录")
    ap.add_argument("--products", required=True, help="文本行产物目录（*.textlines.json）")
    ap.add_argument("--audit", default=None, help="audit 输出目录（默认 <out>/audit）")
    ap.add_argument("--out", default=None, help="输出目录（默认 <pages>/../linefix）")
    ap.add_argument("--backend", default="auto",
                    choices=["auto", "openvino", "onnxruntime"],
                    help="auto=沿用配置文件里的 engine_type")
    ap.add_argument("--pages-filter", nargs="*", default=None,
                    help="只处理指定页（如 page_016 page_030）")
    ap.add_argument("--conf-w2", type=float, default=0.75, help="W2 触发阈值")
    ap.add_argument("--conf-accept", type=float, default=0.85, help="验收 conf 下限")
    ap.add_argument("--pad", type=int, default=6, help="W1 差异框外扩像素")
    ap.add_argument("--pad-line", type=int, default=4, help="W2 原行 bbox 外扩像素")
    ap.add_argument("--fig-ink", type=float, default=C.FIG_INK, help="图版页暗像素占比阈值")
    ap.add_argument("--dry-run", action="store_true", help="只列将处理的目标")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    C.force_utf8_stdout()
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
