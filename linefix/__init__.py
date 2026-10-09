# -*- coding: utf-8 -*-
"""linefix —— 本项目的共用实现层。

**当前只有 `common`**（口径唯一来源）：判据 / 几何与排序 / 中文路径图像 IO /
原子落盘 / 产物 JSON 读写 / 页图身份与过期判定 / 引擎构造与缓存指纹 /
输出目录锁 / 证据归一化 / 字级水平定位。拆页与 OCR 两个阶段都 import 它。

⚠️ 纠错层（漏行审计 `audit` / 局部救援 `rescue` / 字级复核 `recheck`）
**尚未移植** —— 需要时再加。`pipeline/ocr.py` 产出的 `det_boxes`（带 `det_index`
关联）/ `dropped_lines` / `top2` 就是为那几个阶段准备的数据。
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
    "PRODUCT_SCHEMA", "SYMBOL_ONLY", "Line", "OutputLock",
    "accept", "atomic_write_bytes", "atomic_write_text", "build_engine",
    "center_in", "char_x_spans_even", "char_x_spans_from_cols",
    "char_x_spans_from_cols_raw", "default_config_path", "dump_json",
    "dumps_product_json", "engine_fingerprint", "file_sha256",
    "fingerprint_drift", "force_utf8_stdout", "image_drift", "image_fingerprint",
    "imread_u", "imwrite_u", "inc", "iou", "is_figure_page", "iter_alts",
    "list_pages", "load_json", "min_box_thresholds", "ocr_fingerprint",
    "page_size", "read_lines", "rec_line_word_cols", "sort_lines_like_engine",
    "symbol_only", "write_lines", "write_product_json", "xywh",
]

__version__ = "0.4.0"
