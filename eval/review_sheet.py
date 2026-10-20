# -*- coding: utf-8 -*-
"""把复核清单拼成一张对照图，供**人工逐条看原图**判定（台账外优先）。

为什么需要
----------
「台账外」不等于误改 —— 实测第一次跑，唯一的"误改"（page_078「坠人了梦乡」）
复核下来是**台账漏掉的真错**。所以每一条都必须回原图看：打印的到底是哪个字。

用法
----
    ./.venv/Scripts/python.exe eval/review_sheet.py --pages <页图目录> \\
        --review eval/review_book.json --out eval/review_sheet.png
    # 只看台账外的：--only-unknown（默认）／全部：--all
    # 字级定位口径：--loc cols（默认，推荐）／--loc even（旧的"字数均分"，做 A/B 用）

产物
----
一张 PNG：每行一个条目 —— 左边是**原图行裁片**（含上下文，放大到统一高度），
右边是「序号 / 页·行 / 改动 / 比值 / 判定框」。判定框留空，打印出来手写或回填 JSON。

⚠️ 只看数字会被骗：`人/入`、`已/己` 这类差别就在**一两笔**上，必须看像素。

字级定位口径（2026-09-26 改）
----------------------------
`crop_char` 原来用「**字数均分**」估目标字的 x。实测这在**行内混排**（ASCII/数字/空格）时
严重错位：全 34 条纯中文台账上两种口径看不出差别（边界离字间空隙 4.6 vs 4.1 px），
但在 40 条混排行上，均分"边界落在空隙上"的比例只有 **16%**、而端点标定 **43%**
（距离 16.6 → 11.5 px）。⇒ 默认改用 `C.char_x_spans_from_cols`（CTC 时间步 + 端点标定）。

⚠️ 代价：本脚本因此**不再是零推理**（需要按行跑一次 rec-only，引擎就绪 ~1.8s、
每行 ~110ms）。取不到 cols 或文本对不上时**逐条退回均分并计数**，不静默。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import cv2
import numpy as np

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from linefix import common as C  # noqa: E402

ROW_H = 104         # 每行高度（含留白）
CROP_H = 76         # 裁片统一高度
LABEL_W = 620       # 右侧文字区宽度（左侧留给裁片，必须大于裁片宽度）
WIN = 7             # 以目标字为中心，左右各取几个字的窗口

_LOC_ENGINE = None
_IMG_CACHE: Dict[str, Any] = {}


#: 记账转发到唯一实现（`linefix.common.inc`）—— 别在这里再写一份。
inc = C.inc


def loc_engine():
    """字级定位用的 rec_only 引擎（懒加载，只建一次，~1.8s）。

    ⚠️ 两个开关都必须显式给（见 `linefix.common` ⑨ 节）：
       · `use_preprocess_img: False` —— 否则裁片被缩到 ≤2000 且按 32 取整，
         宽高比被扭，`line_txt_len` 也就不再对应**我们给的那张图**
         （实测 2442×93 → 1984×64）。
       · `return_word_box: True` —— 否则拿不到 `word_cols`。
    """
    global _LOC_ENGINE
    if _LOC_ENGINE is None:
        _LOC_ENGINE = C.build_engine(
            backend="auto", rec_only=True,
            extra_params={"Global.use_preprocess_img": False,
                          "Global.return_word_box": True,
                          "Global.return_single_char_box": True},
        )
    return _LOC_ENGINE


def line_image(pages: Path, page: str):
    """整页图。**只缓存当前页** —— 300dpi 页图 3125×4344×3 ≈ 40MB，缓存整批会吃到 GB 级。"""
    if _IMG_CACHE.get("_page") != page:
        _IMG_CACHE.clear()
        _IMG_CACHE["_page"] = page
        _IMG_CACHE["img"] = C.imread_u(pages / f"{page}.png")
    return _IMG_CACHE.get("img")


def char_spans(pages: Path, item: Dict[str, Any], doc: Dict[str, Any], loc: str,
               stat: Dict[str, int]) -> Optional[List[tuple]]:
    """该行**每个字符**的水平区间（原图坐标）。失败返回 None → 调用方退回均分。

    记账口径：`stat` 里只放**可归因的计数**（每条结论都要能指出来自哪个数）。
    """
    ln = (doc.get("lines") or [])[item["line_id"] - 1] if doc else {}
    text = ln.get("text") or ""
    bbox = item.get("bbox")
    if not text or not bbox:
        inc(stat, "无文本/无框")
        return None
    if loc == "even":
        inc(stat, "even(均分)")
        return C.char_x_spans_even(bbox, len(text))

    img = line_image(pages, item["page"])
    if img is None:
        inc(stat, "页图缺失")
        return None
    H, W = img.shape[:2]
    x, y, w, h = [int(v) for v in bbox]
    crop = img[max(0, y - 6): min(H, y + h + 6), max(0, x - 6): min(W, x + w + 6)]
    if crop.size == 0:
        inc(stat, "行裁片为空")
        return None
    try:
        txt, _conf, cols, L = C.rec_line_word_cols(loc_engine(), crop)
    except Exception as e:                      # 引擎异常必须**计数**，不能只落一条日志
        inc(stat, f"重识别异常({type(e).__name__})")
        return None
    if not cols:
        inc(stat, "取不到 cols")
        return None
    if txt != text:
        # ⚠️ **不能**因为"文本不一致"就放弃定位：
        #    实测 34 条台账里有 21 条，原图裁片 rec 出的整行文本与产物不同 ——
        #    但**长度相同、且只有目标位那一个字不同**（原图路径把它读对了，见
        #    `.tmp/rec_arbitrate.py`：20/34 命中、0 连带改动）。字位是对得上的。
        #    真正该拒的是**长度变了**（对齐失效）。
        if len(txt) != len(text):
            inc(stat, "长度变了·拒绝定位")
            return None
        inc(stat, "同长度·内容有差异（照常定位）")
    spans = (C.char_x_spans_from_cols_raw(bbox, cols, L) if loc == "raw"
             else C.char_x_spans_from_cols(bbox, cols))
    if not spans or len(spans) != len(text):
        inc(stat, "cols 区间非法")
        return None
    inc(stat, {"raw": "raw(库绝对口径)", "cols": "cols(端点标定)"}.get(loc, loc))
    return spans


def load_items(path: Path, only_unknown: bool) -> List[Dict[str, Any]]:
    d = C.load_json(path)
    if not d:
        raise SystemExit(f"✗ 读不到复核清单：{path}")
    items = d.get("items") or []
    return [x for x in items if not x.get("known_id")] if only_unknown else items


def crop_char(pages: Path, item: Dict[str, Any], doc: Dict[str, Any],
              spans: Optional[Sequence[tuple]] = None,
              win: int = WIN, crop_h: int = CROP_H) -> Optional[np.ndarray]:
    """裁「目标字 + 上下文」的窗口。

    ⚠️ 不要裁整行：整行太宽、目标字太小，而 `人/入`、`已/己` 的差别就在一两笔上。
    `spans` 给了就用**字级定位**（CTC 时间步，见 `linefix.common` ⑨）；
    没给才退回「字数均分」—— 后者在混排行上会明显错位。
    """
    png = pages / f"{item['page']}.png"
    if not png.exists():
        return None
    img = line_image(pages, item["page"])
    if img is None:
        return None
    bbox = item.get("bbox")
    if not bbox:
        return None
    ln = (doc.get("lines") or [])[item["line_id"] - 1] if doc else {}
    txt = ln.get("text") or ""
    n = max(1, len(txt))
    ci = item["char_index"]
    lo, hi = max(0, ci - win), min(n, ci + win + 1)

    bx, by, bw, bh = [int(v) for v in bbox]
    if spans and len(spans) == n and hi > lo:
        x0, x1 = int(round(spans[lo][0])), int(round(spans[hi - 1][1]))
    else:
        x0 = bx + int(bw * lo / n)
        x1 = bx + int(bw * hi / n)
    pad = 12
    x0, x1 = max(0, x0 - pad), min(img.shape[1], x1 + pad)
    y0, y1 = max(0, by - pad), min(img.shape[0], by + bh + pad)
    crop = img[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    scale = crop_h / max(1, crop.shape[0])
    out = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    lim = LABEL_W - 24                      # 绝不越过标签列
    if out.shape[1] > lim:
        out = out[:, :lim]
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="复核清单 → 原图对照贴图（人工判定用）")
    ap.add_argument("--pages", required=True, help="页图目录（page_*.png）")
    ap.add_argument("--review", default=str(_ROOT / "eval" / "review_book.json"),
                    help="复核清单（`score_glyph.py --scan-thr` 的产物）")
    # ⚠️ 产物目录必须**从 --review 的位置推**，不能硬编码 `eval/products`：
    #    `score_glyph` 把清单写在 `<products>/../review_book.json`，所以
    #    `<review的父目录>/products` 才是**同一批**产物。硬编码的后果是
    #    换一本书 / 换一个产物目录后，行文本悄悄取错源（或取不到），
    #    而表现只是"贴图没有裁片"—— 完全不报错。
    ap.add_argument("--products", default=None,
                    help="产物目录（默认 <--review 的父目录>/products）")
    ap.add_argument("--out", default=str(_ROOT / "eval" / "review_sheet.png"))
    ap.add_argument("--all", action="store_true", help="含台账内条目（默认只看台账外）")
    ap.add_argument("--limit", type=int, default=40, help="最多画多少条（默认 40）")
    ap.add_argument("--win", type=int, default=WIN, help="目标字左右各取几个字（默认 7）")
    ap.add_argument("--crop-h", type=int, default=CROP_H, help="裁片高度（默认 76；判断一两笔之差时调大到 150+）")
    ap.add_argument("--loc", choices=("cols", "even", "raw"), default="cols",
                    help="字级定位口径：cols=CTC 时间步+端点标定（默认，推荐）；"
                         "even=字数均分（旧口径）；raw=库自带绝对换算（有恒定偏移）"
                         "—— 后两个只用于 A/B")
    args = ap.parse_args(argv)
    C.force_utf8_stdout()

    review_path = Path(args.review)
    # 产物目录的取法（按可信度排序，**不猜**）：
    #   ① `--products` 显式给；
    #   ② 清单里记着的 `products`（`score_glyph` 写清单时会带上，那是**同一批**产物）；
    #   ③ 老约定 `<清单父目录>/products`。
    # 本项目产物在 `orign_pdf/textlines`，③ 一定猜错 —— 而猜错的表现只是
    # 「贴图没有裁片」，完全不报错。所以②必须优先，且全都落空要**响亮退出**。
    if args.products:
        products = Path(args.products)
    else:
        spec = C.load_json(review_path) or {}
        products = (Path(spec["products"]) if spec.get("products")
                    else review_path.parent / "products")
    if not products.is_dir():
        print(f"✗ 产物目录不存在：{products}\n"
              f"  它本该是「与 {review_path.name} 同源的产物」。\n"
              f"  用 --products 显式指定，别让它去猜 —— 猜错的表现只是"
              f"「贴图没有裁片」，不报错。")
        return 2

    items = load_items(review_path, only_unknown=not args.all)
    n_all = len(items)
    items = items[: args.limit]
    if not items:
        print("ℹ 台账外条目为 0 —— 无需复核（或改用 --all 看全部）")
        return 0
    print(f"清单条目 {n_all} 条，本次画 {len(items)} 条"
          f"（{'全部' if args.all else '仅台账外'}）")

    pages = Path(args.pages)
    W = LABEL_W + 780
    rows: List[np.ndarray] = []
    n_nodoc: List[str] = []
    n_nocrop: List[str] = []
    stat: Dict[str, int] = {}

    for i, it in enumerate(items, 1):
        row = np.full((max(ROW_H, args.crop_h + 28), W, 3), 255, np.uint8)
        doc = C.load_json(products / f"{it['page']}.textlines.json")
        if doc is None:                 # 缺行文本 → 裁片定位不了，记账后响亮报出
            n_nodoc.append(f"{it['page']}")
            doc = {}
        spans = char_spans(pages, it, doc, args.loc, stat)
        if spans is None and args.loc != "even":
            inc(stat, "── 退回均分")
        crop = crop_char(pages, it, doc, spans=spans, win=args.win, crop_h=args.crop_h)
        if crop is None:
            n_nocrop.append(f"{it['page']} L{it['line_id']}")
        if crop is not None:
            ch, cw = crop.shape[:2]
            y0 = max(0, (row.shape[0] - ch) // 2)
            row[y0:y0 + ch, 10:10 + cw] = crop
        cv2.line(row, (LABEL_W, 0), (LABEL_W, row.shape[0]), (200, 200, 200), 1)
        rows.append(row)

    # 用 PIL 画中文标签（cv2 不支持中文）
    from PIL import Image, ImageDraw, ImageFont
    # 不能只试两个路径：换台机器就可能一个都没有（与 tests/ 里的字体查找同口径）。
    font_file = next((p for p in (r"C:\Windows\Fonts\msyh.ttc",
                                  r"C:\Windows\Fonts\simhei.ttf",
                                  r"C:\Windows\Fonts\simsun.ttc",
                                  r"C:\Windows\Fonts\Deng.ttf") if Path(p).is_file()), None)
    if font_file is None:
        raise SystemExit("✗ 找不到中文字体（msyh.ttc / simhei.ttf / simsun.ttc / Deng.ttf）")
    f_big = ImageFont.truetype(font_file, 20)
    f = ImageFont.truetype(font_file, 17)

    canvas = Image.fromarray(cv2.cvtColor(np.vstack(rows), cv2.COLOR_BGR2RGB))
    dr = ImageDraw.Draw(canvas)
    for i, it in enumerate(items, 1):
        y = sum(r.shape[0] for r in rows[: i - 1])
        dr.text((LABEL_W + 14, y + 12), f"#{i}  {it['page']} L{it['line_id']} pos{it['char_index']}",
                font=f_big, fill=(0, 0, 0))
        dr.text((LABEL_W + 14, y + 40),
                f"{it['before']} → {it['new']}   r={it['ratio']:.3f}   "
                f"{it.get('known_id') or '台账外'}", font=f, fill=(120, 60, 0))
        dr.text((LABEL_W + 14, y + 63), it.get("context", "")[:44], font=f, fill=(60, 60, 60))
        dr.text((LABEL_W + 14, y + 85),
                "判定: [ ]真错(补进评测集)  [ ]误改(加约束)", font=f, fill=(0, 90, 0))
        dr.line([(0, y), (canvas.width, y)], fill=(220, 220, 220), width=1)

    out = Path(args.out)
    canvas.save(out)
    print(f"贴图 -> {out}  （{canvas.width}×{canvas.height}）")
    print(f"  行文本取自 {products}／页图取自 {pages}")
    print(f"  字级定位（--loc {args.loc}）逐条记账：")
    for k, v in sorted(stat.items(), key=lambda kv: -kv[1]):
        print(f"    {k:<22} {v:>4}")
    if n_nodoc:
        print(f"  ⚠ {len(n_nodoc)} 条在产物目录里找不到对应页（这些条的裁片会缺失）："
              f"{', '.join(sorted(set(n_nodoc))[:6])}"
              f"{' …' if len(set(n_nodoc)) > 6 else ''}")
    if n_nocrop:
        print(f"  ⚠ {len(n_nocrop)} 条没能裁出图（bbox 缺失 / 页图缺失 / 定位越界）："
              f"{', '.join(n_nocrop[:6])}{' …' if len(n_nocrop) > 6 else ''}")
    if n_nodoc or n_nocrop:
        print("  ⇒ 缺裁片的条目**不能靠这张图判定** —— 先修好来源再复核。")
    print("看原图逐条判：**打印的是哪个字** —— 这类差别只在一两笔上，数字看不出来。")
    return 1 if (n_nodoc or n_nocrop) else 0


if __name__ == "__main__":
    raise SystemExit(main())
