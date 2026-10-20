# -*- coding: utf-8 -*-
"""linefix —— 文本行级「漏行审计 + 局部救援 + 字级定向修复」套件。

本包分两层：**共用实现层**（`common`，口径唯一来源）与**三个业务模块**。
拆页 / OCR / 纠错三个阶段都 import `common`，判据只写一次。

模块职责
--------
- `linefix.common` —— 判据 / 几何与排序 / 中文路径图像 IO / 原子落盘 / 产物 JSON 读写 /
  页图身份与过期判定 / 引擎构造与缓存指纹 / 输出目录锁 / 证据归一化 /
  字级水平定位（CTC 时间步 → x）。
- `linefix.audit`   —— **零推理**：只读产物的 `det_boxes`，与产物行做空间集合差，
  产出「有框无行」清单；并用 `dropped_lines[].det_index` 做交叉核对。
- `linefix.rescue`  —— 对差异框（W1）/ 低置信度行（W2），从**原图**裁精确框做
  rec-only 重识别（行级），验收后写成修补产物（非破坏性）。
- `linefix.recheck` —— **字级**：对 `top2` 标出的可疑位（形近字摇摆 / 疑似无字）
  做原图二次后验，按**结构式**规则定向修复；候选从数据现取，不维护人工混淆对表。

设计原则（全部有实测支撑，别重新推一遍）
----------------------------------------
1. **判定 ≠ 采纳**：`judge` 只给结论，`should_adopt` 决定写不写盘。
2. **提升小的不用**：标点全半角 / 噪点 / 空白多寡，一律不采纳。
3. **对原文内容有损害的不用**：**任何删除都不采纳**（默认 `--adopt cjk`）。
4. **响亮失败优于静默降级**：缺字段 / 对不齐 / 长度变了 → 拒绝并计数，不硬凑。
5. **不用文本反查证据**：`top2` 用字符下标定位、`det_boxes` 用 `det_index` 关联，
   一律靠稳定 ID，不靠"文本多重集"猜（同页重复文本时会挂错证据）。
6. **产物一律走 `write_product_json` / `write_lines`** —— `top2` / `det_boxes`
   这类逐点数组必须压成单行，否则一页能膨胀到 4000+ 行 JSON。

产物口径
--------
沿用 `{"page", "line_count", "lines":[{"line_id","bbox","text","confidence"}]}`，
并叠加：`orig_line_id` / `added`（救援）、`det_index` / `role`（OCR，schema 6）、
`top2`（逐字概率）、`text_corrections`（定向修复留痕）。
改动 JSON 结构时必须 +`PRODUCT_SCHEMA`，否则旧产物会被当可复用、静默混格式。
"""

from .common import (  # noqa: F401
    CTC_PX_PER_STEP,
    ENGINE_Y_THRESHOLD,
    FIG_INK,
    FIG_INK_GRAY_CUT,
    MIN_BOX_H_ABS,
    MIN_BOX_H_RATIO,
    MIN_BOX_W_ABS,
    MIN_BOX_W_RATIO,
    PAGE_NO_TEXT,
    PAGE_NO_Y,
    PRODUCT_SCHEMA,
    SYMBOL_ONLY,
    Line,
    OutputLock,
    accept,
    atomic_write_bytes,
    atomic_write_text,
    build_engine,
    center_in,
    char_x_spans_even,
    char_x_spans_from_cols,
    char_x_spans_from_cols_raw,
    default_config_path,
    dump_json,
    dumps_product_json,
    engine_fingerprint,
    file_sha256,
    fingerprint_drift,
    force_utf8_stdout,
    image_drift,
    image_fingerprint,
    imread_u,
    imwrite_u,
    inc,
    iou,
    is_figure_page,
    iter_alts,
    list_pages,
    load_json,
    min_box_thresholds,
    ocr_fingerprint,
    page_number_role,
    page_size,
    read_lines,
    rec_line_word_cols,
    sort_lines_like_engine,
    symbol_only,
    write_lines,
    write_product_json,
    xywh,
)

__all__ = [
    "CTC_PX_PER_STEP", "ENGINE_Y_THRESHOLD", "FIG_INK", "FIG_INK_GRAY_CUT",
    "MIN_BOX_H_ABS", "MIN_BOX_H_RATIO", "MIN_BOX_W_ABS", "MIN_BOX_W_RATIO",
    "PAGE_NO_TEXT", "PAGE_NO_Y", "PRODUCT_SCHEMA", "SYMBOL_ONLY", "Line",
    "OutputLock",
    "accept", "atomic_write_bytes", "atomic_write_text", "build_engine",
    "center_in", "char_x_spans_even", "char_x_spans_from_cols",
    "char_x_spans_from_cols_raw", "default_config_path", "dump_json",
    "dumps_product_json", "engine_fingerprint", "file_sha256",
    "fingerprint_drift", "force_utf8_stdout", "image_drift", "image_fingerprint",
    "imread_u", "imwrite_u", "inc", "iou", "is_figure_page", "iter_alts",
    "list_pages", "load_json", "min_box_thresholds", "ocr_fingerprint",
    "page_number_role", "page_size", "read_lines", "rec_line_word_cols",
    "sort_lines_like_engine", "symbol_only", "write_lines", "write_product_json",
    "xywh",
]

__version__ = "0.5.0"
