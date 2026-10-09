# -*- coding: utf-8 -*-
"""产线各阶段共用的唯一实现层。

这里集中了原来在 02c / 02d 里各写一份、以及各阶段各写一份的三类东西：
  ① **判据**：图片页、纯符号行、验收规则
  ② **口径**：行排序（对齐引擎 `_BOX_SORT_Y_THRESHOLD`）、bbox 几何、
     产物 JSON 的序列化、读产物的两代口径、字级定位（CTC 时间步 → x）
  ③ **基建**：中文路径安全读写、**原子写**、**输出目录锁**、引擎构造与缓存指纹、
     **页图身份 / 产物过期判定**

**口径只写一份**：这些一旦各阶段各写一套，就会出现"同一份数据两处口径不同"的经典失败
（本项目历史上 02c/02d 各复制一份，与 `load_tl_aligned` 两份实现是同一个坑）。

模块职责分区（改动时对照着看）：
    ⓪ 常数            ——— 所有"魔法数"只出现一次
    ① 判据 / 契约      ——— 什么算图版页、什么算纯符号行、产物行长什么样
    ② 几何与排序       ——— bbox、IoU、按引擎口径排序
    ③ 中文路径图像 IO  ——— cv2 在中文路径会返回 None，统一从这走
    ④ 原子落盘         ——— 半截文件绝不能留给下游
    ⑤ 产物 JSON 读写   ——— 逐点数组压行；**严格校验**坏产物
    ⑥ 页图身份 / 过期   ——— 判"产物还是不是这份页图的"
    ⑦ 引擎构造与指纹    ——— 复用引擎实例；口径指纹用于断点续跑判据
    ⑧ 输出目录锁       ——— 清单是 read-modify-write，不许并发
    ⑨ 杂项            ——— 计数记账、UTF-8 stdout、列页名
    ⑩ 证据归一化       ——— `top2` / `alt_candidates` 两代口径的统一读取
    ⑪ 字级水平定位     ——— "这个字在原图哪儿"的唯一实现
"""
from __future__ import annotations

import hashlib
import importlib.metadata as md
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, TypedDict, Union

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# ⓪ 常数：所有"魔法数"只在这里出现一次
# ---------------------------------------------------------------------------
#: `os.replace` 撞上 Windows 的"目标文件被别的句柄占用"时重试几次、每次等多久。
#: 5 × 20ms 足够覆盖"读方只是短暂打开一下"；再长说明是真冲突，交给调用方去响。
REPLACE_RETRIES = 5
REPLACE_RETRY_SLEEP = 0.02

#: 行排序换行阈值。**必须与引擎一致** —— rapidocr `ch_ppocr_det/main.py` 用
#: `_BOX_SORT_Y_THRESHOLD = 10`（dy >= 10 才算换行，行内再按 x 排）。
ENGINE_Y_THRESHOLD = 10

#: 图片页判据：暗像素占比超过此值视为图版页（艺术字 rec 必然失败，非漏行）。
FIG_INK = 0.45
FIG_INK_GRAY_CUT = 160

#: 纯符号行（含中文标点）—— 正则用 \W，配合 UNICODE 覆盖中文标点。
SYMBOL_ONLY = re.compile(r"^[\W_]+$", re.UNICODE)

#: 差异框尺寸门槛。**按页宽/页高归一化**，避免换书就要手调
#: （原版是 02c 里的裸像素 60/20，对 2126px 与 4167px 宽的书不可能同时合适）。
MIN_BOX_W_RATIO = 0.020
MIN_BOX_H_RATIO = 0.006
MIN_BOX_W_ABS = 24        # 绝对下限，防止极小页把阈值压到无意义
MIN_BOX_H_ABS = 10


class Line(TypedDict, total=False):
    """产物里的一行文本。前四项是既有约定，后两项是救援阶段新增。"""
    line_id: int                 # 当前产物空间的行号（1..n）
    bbox: List[float]            # [x, y, w, h]，**原图坐标**
    text: str
    confidence: Optional[float]
    orig_line_id: Optional[int]  # 重排前它在**源产物**里的行号；新增行=None
    added: bool                  # 是否为救援时新插入的行


# ---------------------------------------------------------------------------
# ① 判据 / 契约
# ---------------------------------------------------------------------------
def symbol_only(text: str) -> bool:
    """是否纯符号行（含中文标点）。"""
    t = (text or "").strip()
    return True if not t else bool(SYMBOL_ONLY.match(t))


def accept(
    new_text: str, new_conf: float, old_len: int = 0,
    conf_accept: float = 0.85,
) -> Tuple[bool, str]:
    """验收：conf 达标 + 非空 + 非纯符号 + 字符数不缩水。返回 (通过, 原因)。"""
    if new_conf < conf_accept:
        return False, f"conf {new_conf:.3f} < {conf_accept}"
    if not (new_text or "").strip():
        return False, "空文本"
    if symbol_only(new_text):
        return False, "纯符号"
    if old_len and len(new_text) < old_len:
        return False, f"字符数缩水 {len(new_text)} < {old_len}"
    return True, ""


def is_figure_page(
    png_path: Union[str, Path],
    ink_thresh: float = FIG_INK,
    cache: Optional[Dict[str, Optional[bool]]] = None,
) -> Optional[bool]:
    """图片页判据：暗像素占比 > 阈值。

    返回 **`Optional[bool]`**：`True` = 图版页 / `False` = 不是 / **`None` = 判不了**。

    🔴 为什么不能吞成 `False`：解码失败、路径不存在、PIL 不可用、权限/内存问题……
    这些都只是"**无法判断**"，一旦当成"**确定不是**图版页"，下游就会把一张读出
    艺术字失败的图版页当成"漏行可疑页"上报（或反过来漏掉）。两种误判都会污染
    后续的人工核查。本项目在 `rec_line_word_cols` 那侧已经确立"响亮失败优于静默降级"，
    这里必须用同一套口径 —— 由调用方决定"判不了"该怎么记。

    `cache` 传入一个 dict 即可复用结果（同一页在多处扫描时只算一次）。
    cache 的 key 做**绝对路径归一化**，否则 `./pages/a.png` 与 `pages/a.png` 会被当成
    两个条目，缓存形同虚设。
    """
    key = os.path.normcase(os.path.abspath(str(png_path)))
    if cache is not None and key in cache:
        return cache[key]
    from PIL import Image
    try:
        with Image.open(png_path) as im:
            g = np.asarray(im.convert("L"))
        val: Optional[bool] = bool((g < FIG_INK_GRAY_CUT).mean() > ink_thresh)
    except Exception:
        val = None                     # 判不了 ≠ 不是
    if cache is not None:
        cache[key] = val
    return val


# ---------------------------------------------------------------------------
# ② 几何与排序
# ---------------------------------------------------------------------------
def xywh(box) -> Tuple[float, float, float, float]:
    """四点框 / 任意 (N,2) 形状 → (x, y, w, h)。"""
    b = np.asarray(box, dtype=float).reshape(-1, 2)
    x0, y0 = float(b[:, 0].min()), float(b[:, 1].min())
    x1, y1 = float(b[:, 0].max()), float(b[:, 1].max())
    return x0, y0, x1 - x0, y1 - y0


def iou(a: Sequence[float], b: Sequence[float]) -> float:
    """两个 (x, y, w, h) 的 IoU。"""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def center_in(inner: Sequence[float], outer: Sequence[float], pad: float = 0) -> bool:
    """inner 的中心点是否落在 outer（外扩 pad）内。"""
    x, y, w, h = inner
    ox, oy, ow, oh = outer
    cx, cy = x + w / 2, y + h / 2
    return (ox - pad) <= cx <= (ox + ow + pad) and (oy - pad) <= cy <= (oy + oh + pad)


def _require_bbox(line: Any, where: str) -> Tuple[float, float, float, float]:
    """取 `line["bbox"]` 并校验契约：必须是 4 个可转 float 的数。

    🔴 **不宽松处理**：`bbox` 是产物里一切空间运算（排序、IoU、裁片、对账）的输入，
    出现 `null` / `[1]` / 字符串时就该当场炸在**离源头最近的地方**，而不是让排序函数
    抛一个 `TypeError: 'NoneType' is not subscriptable` 让人去猜哪条产物坏了。
    """
    if not isinstance(line, dict):
        raise ValueError(f"{where}: 期望 dict，实得 {type(line).__name__}")
    bb = line.get("bbox")
    if not isinstance(bb, (list, tuple)) or len(bb) < 4:
        raise ValueError(f"{where}: bbox 必须是 [x, y, w, h]，实得 {bb!r}")
    try:
        return (float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3]))
    except (TypeError, ValueError):
        raise ValueError(f"{where}: bbox 含非数值：{bb!r}") from None


def sort_lines_like_engine(
    lines: List[Line], y_threshold: int = ENGINE_Y_THRESHOLD
) -> List[Line]:
    """按**引擎口径**排序：先 y 稳定排序，`dy >= y_threshold` 才算换行，行内按 x。

    与引擎 `_BOX_SORT_Y_THRESHOLD` 对齐（原版 02d 用精确 `(y, x)` 元组排序，
    同页有框 y 差 3px 时结果就可能与主链路不一致）。

    ⚠️ 输入契约：每个元素的 `bbox` 必须是 `[x, y, w, h]`（可转 float）。
    不满足时抛 `ValueError` 并指出是第几个元素 —— **不做静默跳过**，
    因为"少排了一行"比"响亮报错"难查得多。
    """
    for i, L in enumerate(lines):
        _require_bbox(L, f"sort_lines_like_engine: lines[{i}]")
    seq = sorted(lines, key=lambda L: float(L["bbox"][1]))
    groups: List[List[Line]] = []
    cur: List[Line] = []
    prev_y: Optional[float] = None
    for L in seq:
        y = float(L["bbox"][1])
        if prev_y is None or (y - prev_y) >= y_threshold:
            if cur:
                groups.append(cur)
            cur = [L]
        else:
            cur.append(L)
        prev_y = y
    if cur:
        groups.append(cur)

    out: List[Line] = []
    for g in groups:
        out.extend(sorted(g, key=lambda L: float(L["bbox"][0])))
    return out


def min_box_thresholds(page_w: int, page_h: int,
                       w_ratio: float = MIN_BOX_W_RATIO,
                       h_ratio: float = MIN_BOX_H_RATIO) -> Tuple[int, int]:
    """按页尺寸算出该页的最小差异框宽高。"""
    return (max(MIN_BOX_W_ABS, int(w_ratio * page_w)),
            max(MIN_BOX_H_ABS, int(h_ratio * page_h)))


# ---------------------------------------------------------------------------
# ③ 中文路径安全的图像 IO
# ---------------------------------------------------------------------------
def imread_u(path: Union[str, Path], flags: int = cv2.IMREAD_COLOR):
    """中文路径安全的读图。

    ⚠️ `cv2.imread` 在中文路径返回 None（OpenCV 已知限制），本项目所有路径含中文。
    ⚠️ 读出来是 **BGR**，直接传 ndarray 给引擎时它**不会**再做 RGB2BGR 转换。
    """
    try:
        buf = np.fromfile(str(path), dtype=np.uint8)
        if buf.size == 0:
            return None
        return cv2.imdecode(buf, flags)
    except Exception:
        return None


def imwrite_u(path: Union[str, Path], img) -> bool:
    """中文路径安全的写图。**原子写** —— 与 `atomic_write_bytes` 同一套约定。

    🔴 为什么要改：本模块顶部声明"所有写盘都走原子写"，而早期实现直接
    `buf.tofile(最终路径)` —— 下游（OCR / 审计 / 扫描器）可能在这张图**还没写完**
    的时候就看到它，于是把半截图当合法产物。整个流水线对"半截产物"的防护
    必须没有例外，否则它就不成其为约定。
    """
    p = Path(path)
    ext = os.path.splitext(str(p))[1] or ".png"
    ok, buf = cv2.imencode(ext, img)
    if not ok:
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        buf.tofile(str(tmp))
        _replace_with_retry(tmp, p)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return True


def page_size(png_path: Union[str, Path]) -> Optional[Tuple[int, int]]:
    """(w, h)。只读文件头，不解码整图。"""
    try:
        from PIL import Image
        with Image.open(png_path) as im:
            return int(im.width), int(im.height)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# ④ 原子落盘：**原子写**是全局约定（半截文件绝不能留给下游）
# ---------------------------------------------------------------------------
def _replace_with_retry(tmp: Path, p: Path) -> None:
    """`os.replace` + Windows 上的有界重试。

    ⚠️ Windows 上 `os.replace` 在**目标文件正被别的句柄打开**时会抛 `PermissionError`
    （读方并发读就会触发，实测踩到）。这不是数据问题，所以重试；
    重试耗尽仍原样抛出 —— 不吞异常，读方该看到的失败照旧看得到。
    """
    for attempt in range(REPLACE_RETRIES):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            if attempt == REPLACE_RETRIES - 1:
                raise
            time.sleep(REPLACE_RETRY_SLEEP)


def atomic_write_bytes(path: Union[str, Path], data: bytes) -> None:
    """原子写二进制：先写**唯一名**临时文件，再 `os.replace`。

    ⚠️ 为什么不能省：半截 JSON 的危害不是"多一个坏文件"，而是**下游可能把它当合法产物**。
    本项目所有写盘（页图、产物、清单）都走这里，口径只写一份。

    ⚠️ 临时文件名**必须唯一**（pid + 线程 id）：固定名 `<名>.tmp` 在两个进程同时写同一
    路径时会互相覆盖对方的临时文件，`os.replace` 的原子性只保证"要么旧的要么新的"，
    保证不了"新的完整"。真正防并发靠**输出目录锁**（`OutputLock`，各阶段入口都在用），
    唯一名是第二道保险 —— 锁没覆盖到的地方（例如库被直接调用）也不会写坏。

    ⚠️ 反向也成立：写方正在 `os.replace` 的那一瞬间，**读方 `open()` 也可能被拒**
    （ERROR_ACCESS_DENIED）。所以本项目的读方若可能与写方并发，必须容忍这个瞬时失败。
    正常用法是各阶段入口的输出目录锁把读写错开，不是让每个读点自己重试。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_bytes(data)
        _replace_with_retry(tmp, p)
    except BaseException:
        # 失败时别把半截临时文件留在目录里（下游的 glob 迟早会扫到它）。
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_text(path: Union[str, Path], text: str) -> None:
    """原子写文本（UTF-8）。"""
    atomic_write_bytes(path, text.encode("utf-8"))


def file_sha256(path: Union[str, Path], chunk: int = 1 << 20) -> str:
    """文件内容哈希。用于"这份产物对应的到底是哪一版输入"这类判定。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# ⑤ 产物 JSON 读写（格式唯一来源）
# ---------------------------------------------------------------------------
# 「逐点数组」字段：这些是每行/每字一条记录的稠密数组，照常缩进会把产物撑成几千行。
_COMPACT_KEYS = ("det_boxes", "top2")


def dumps_product_json(doc: Dict[str, Any], indent: int = 1) -> str:
    """序列化文本行产物：**逐点数组压成单行**，其余字段照常缩进。

    为什么不直接 `json.dumps(indent=1)`：`top2` 是**每个字**一条（一页约 600 条），
    默认缩进会把每个条目摊成 7 行 —— 一页 25 行文本变成 4000+ 行 JSON，
    产物就没法看了（实测 page_099：4280 行）。`det_boxes` 同理。

    ⚠️ 膨胀的是**行数**不是字节（indent 只加空格换行）。所以"体积不大"不是不管它的理由 ——
    受影响的是 grep / diff / 编辑器 / 工具截断 / 人工抽查。

    实现：先把这些数组换成哨兵字符串，整体序列化后再原样替换回紧凑 JSON。
    哨兵含 NUL（OCR 字符表里 NUL 是 blank，永远不会出现在输出文本里），
    且比较的是**转义后**的形式，所以不会误伤正文。

    ⚠️ 这是个**格式 hack**，不是普通 `json.dumps()`：它依赖"正文里不会出现哨兵那样的
    转义序列"。所以末尾带**逐值自校验**，一旦前提不成立就**拒绝落盘**，
    而不是写出一份被悄悄改坏的产物。
    """
    holders: Dict[str, str] = {}
    out: Dict[str, Any] = dict(doc)

    def stash(arr) -> str:
        raw = f"\x00P\x00{len(holders)}\x00"
        holders[f'"{json.dumps(raw)[1:-1]}"'] = json.dumps(
            arr, ensure_ascii=False, separators=(",", ":"))
        return raw

    for k in _COMPACT_KEYS:
        if out.get(k):
            out[k] = stash(out[k])
    if out.get("lines"):
        lines2 = []
        for ln in out["lines"]:
            ln = dict(ln)
            if ln.get("top2"):                 # 放回末尾：正文在前、概率在后更好读
                ln["top2"] = stash(ln.pop("top2"))
            lines2.append(ln)
        out["lines"] = lines2
    if out.get("dropped_lines"):
        drops2 = []
        for d in out["dropped_lines"]:
            d = dict(d)
            if d.get("top2"):
                d["top2"] = stash(d.pop("top2"))
            drops2.append(d)
        out["dropped_lines"] = drops2

    s = json.dumps(out, ensure_ascii=False, indent=indent)

    # ---- 自校验：这个 hack 一旦失效，产物会被**安静地改坏**，必须挡住 ----
    for k, v in holders.items():
        if k not in s:
            raise RuntimeError(
                f"紧凑序列化失败：哨兵 {k!r} 没有出现在中间产物里（本函数内部 bug）")
        s = s.replace(k, v)
    # 逐值对账：解析回来必须与"直接 json.dumps"的语义**完全一致**。
    # 只验"能解析"是不够的 —— 哨兵被正文里的同形转义序列误伤时，JSON 依然是合法的。
    try:
        if json.loads(s) != json.loads(json.dumps(doc, ensure_ascii=False)):
            raise RuntimeError("紧凑序列化后内容与原文不一致（哨兵被正文误伤）")
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(
            f"紧凑序列化后无法解析（产物已损坏，拒绝落盘）：{type(e).__name__}: {e}") from e
    return s


def write_product_json(path: Union[str, Path], doc: Dict[str, Any]) -> None:
    """原子写文本行产物（紧凑逐点数组）。**产物一律走这里**，别直接 json.dumps。"""
    atomic_write_text(path, dumps_product_json(doc))


def read_lines(path: Union[str, Path]) -> Optional[Dict[str, Any]]:
    """读文本行产物。**缺失/损坏一律返回 None**（不抛异常、也不返回半截结构）。

    🔴 这里是"损坏"的**唯一判定点**，所以校验必须到位：
    早期实现只判 `"lines" not in d`，于是 `{"lines": {}}`（dict）或 `{"lines": null}`
    会一路走到 `ln.setdefault(...)` 才炸 —— docstring 说"损坏返回 None"，
    实际却抛异常，调用方的 `except` 与否决定了它是崩还是静默错。
    现在：顶层必须是 dict、`lines` 必须是 list、每个元素必须是 dict。
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    lines = d.get("lines")
    if not isinstance(lines, list):
        return None
    for ln in lines:
        if not isinstance(ln, dict):
            return None
    # 补齐新增字段的默认值，使下游无需判 None
    for ln in lines:
        ln.setdefault("orig_line_id", ln.get("line_id"))
        ln.setdefault("added", False)
    return d


def write_lines(
    path: Union[str, Path],
    page: str,
    lines: List[Line],
    provenance: Dict[str, Any],
    orig_line_count: Optional[int] = None,
    added: int = 0,
    det_boxes: Optional[List[list]] = None,
    dropped_lines: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """写文本行产物（含 `orig_line_id` / `added` / `provenance`）。**原子写**。

    ⚠️ `det_boxes` 必须**从源产物透传**：det 框与 rec 无关，救援只改文本行，
    框一个都没动。不透传的话修补后的产物会缺这个字段，
    下一次审计只能把它当"生产者没接上"跳过 —— 闭环就断了。

    ⚠️ 行里的 `top2`（每个字的 top-2 概率）原样带着走 —— 救援只改个别字，
    其余字的概率证据不该丢（丢了就没法对修补后的产物再做形近字分析）。
    `dropped_lines` 是**历史快照**，原样透传，不代表救援后的残留。
    """
    doc: Dict[str, Any] = {
        "page": page,
        "engine": "linefix.v6+rescue",
        "line_count": len(lines),
        "orig_line_count": orig_line_count,
        "added": added,
        "provenance": provenance,
        "lines": lines,
    }
    if det_boxes is not None:
        doc["det_boxes"] = det_boxes
    if dropped_lines is not None:
        doc["dropped_lines"] = dropped_lines
    write_product_json(path, doc)


def load_json(path: Union[str, Path], default=None):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def dump_json(path: Union[str, Path], obj, indent: int = 1) -> None:
    """写 JSON。**原子写**（清单/汇总也是产物，半截同样会骗到下游）。"""
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=indent))


# ---------------------------------------------------------------------------
# ⑥ 页图身份与"产物是否过期"
# ---------------------------------------------------------------------------
def image_fingerprint(png: Union[str, Path]) -> Dict[str, Any]:
    """页图身份：字节数 + mtime_ns + 内容哈希。写进产物 `provenance`。

    ⚠️ 已知代价：每次调用都会读整张 PNG 算 SHA256，而 `image_drift()` 复核时
    还会再算一遍。几十万字以内无所谓；页数上万时再加"按 (size, mtime_ns) 缓存"，
    现在不加，因为**多一层缓存就多一个"错判过期"的机会**。
    """
    p = Path(png)
    st = p.stat()
    return {
        "png_bytes": int(st.st_size),
        "png_mtime_ns": int(st.st_mtime_ns),
        "png_sha256": file_sha256(p),
    }


def image_drift(prov, png: Union[str, Path]) -> Optional[str]:
    """产物记录的页图身份与**当前**页图是否一致。不一致返回原因，一致返回 None。

    这是"产物是否过期"的唯一判据。判序：先比字节数（几乎免费），相同再比 SHA256 ——
    于是「重新渲染出**完全相同**的 PNG」（同参数重跑拆分）不会被误判为过期，
    而「真的换了图」（换 DPI / 换 PDF / 换页序）一定被抓到。

    ⚠️ 不这么做就会静默交付过期产物：只判"JSON 能解析"的话，
    重渲染页图后重跑 OCR 会**整本跳过**，而产物里还是旧页的文本。

    🔴 **页图不存在也返回非 None**（`"页图不存在：..."`）。早期实现这里返回 `None`，
    而调用方普遍把 `None` 读成"一致"——于是一张**缺失**的页图会被当成"产物没问题"，
    正好绕过整个过期守卫。`None` 只允许表示一件事：**比过了，且一致**。
    """
    if not isinstance(prov, dict):
        return "产物没有 provenance"
    if not prov.get("png_sha256"):
        return "产物没记页图哈希（旧版本产物）"
    try:
        st = Path(png).stat()
    except OSError:
        return f"页图不存在：{png}"
    if int(prov.get("png_bytes") or -1) != int(st.st_size):
        return f"页图字节数变了（{prov.get('png_bytes')} → {st.st_size}）"
    if prov["png_sha256"] != file_sha256(png):
        return "页图内容变了（SHA256 不符）"
    return None


# ---------------------------------------------------------------------------
# ⑦ 引擎构造与缓存指纹
# ---------------------------------------------------------------------------
DEFAULT_CONFIG_REL = "configs/rapidocr_openvino_ppocrv6_medium.yaml"

#: 产物**布局**版本：改动 JSON 结构（字段增删 / 语义变化）就 +1。
#: 存在的意义：把"格式变了、旧产物却被当可复用"从静默混格式变成响亮重跑。
#:   4 → 5：OCR 产物新增 `lines[].det_index` / `dropped_lines[].det_index`，
#:          并把 top2 对齐口径从"文本多重集"改成"det 下标"（见 pipeline/ocr.py）。
PRODUCT_SCHEMA = 5


def default_config_path() -> Optional[Path]:
    """本项目的 rapidocr 完整配置（引擎/模型档位的唯一来源）。"""
    p = Path(__file__).resolve().parents[1] / DEFAULT_CONFIG_REL
    return p if p.exists() else None


def _resolve_engine_type(backend: str):
    """'auto' → None（沿用配置文件）；'openvino'/'onnxruntime' → EngineType。

    未知取值**抛错不静默**（避免"以为换了后端其实没换"）。
    """
    b = (backend or "auto").strip().lower()
    if b in ("auto", "", "default"):
        return None
    if b not in ("openvino", "onnxruntime"):
        raise ValueError(f"未知后端 {backend!r}（可选：auto / openvino / onnxruntime）")
    from rapidocr.utils.typings import EngineType
    return EngineType.OPENVINO if b == "openvino" else EngineType.ONNXRUNTIME


def build_engine(
    backend: str = "auto",
    rec_only: bool = False,
    config_path: Optional[Union[str, Path]] = None,
    log_level: str = "warning",
    extra_params: Optional[Dict[str, Any]] = None,
):
    """构造 rapidocr 引擎。

    Args:
        backend: `auto` 沿用配置文件的 `engine_type`；否则强制三段覆盖。
        rec_only: True 时关掉 det / cls / vertical_padding —— 单行裁片重识别用。
        config_path: 完整配置文件；None 时自动找本项目 `configs/` 下的那份。
        log_level: rapidocr 日志级别（warning 可压掉 INFO 噪音）。
        extra_params: 额外的 `params` 覆盖，**最后合并（调用方优先）**。
            给"某个调用点需要特殊开关"用，避免给每个开关都长一个形参。已用到：
            · 字级定位 —— `Global.use_preprocess_img: False` + `Global.return_word_box: True`
              （原因见 `rec_line_word_cols` 的说明）。

    ⚠️ `use_cls=False` **不会**阻止 TextClassifier 被构造，所以 Cls 段的
       `engine_type` 也必须指向已安装的后端（否则 ImportError）。
    ⚠️ 用 `params=` 时枚举值必须传 `EngineType` 枚举，传字符串会 TypeError。
    ⚠️ 生产环境**复用同一个实例**：OpenVINO 初始化约 1s（首次含编译约 7.5s），
       不要每条文本重建。
    """
    from rapidocr import RapidOCR

    params: Dict[str, Any] = {"Global.log_level": log_level}
    if rec_only:
        # 单行图无需再检测/角度分类；vertical padding 会把 1000x60 的行切片
        # 上下补白（实测打到 250px 高），坐标全偏 → 必须关掉。
        params.update({
            "Global.use_det": False,
            "Global.use_cls": False,
            "Global.use_vertical_padding": False,
        })
    if extra_params:
        params.update(extra_params)

    eng = _resolve_engine_type(backend)
    if eng is not None:
        for sec in ("Det", "Rec", "Cls"):
            params[f"{sec}.engine_type"] = eng

    cfg = Path(config_path) if config_path else default_config_path()
    cfg_arg = str(cfg) if cfg and Path(cfg).exists() else None
    return RapidOCR(config_path=cfg_arg, params=params)


def _versions() -> Dict[str, str]:
    """运行时关键组件版本。**必须进指纹** —— 包被升级而指纹没动，旧产物会被错误复用。"""
    out: Dict[str, str] = {}
    for pkg in ("rapidocr", "openvino", "numpy", "opencv-python"):
        try:
            out[pkg] = md.version(pkg)
        except Exception:
            out[pkg] = "?"
    return out


def _model_files(c) -> Dict[str, str]:
    """引擎实际会加载的**权重文件**的 SHA256（按引擎自己的解析函数复算路径）。

    为什么要算实际哈希而不是只记模型名：模型名相同、文件被换掉（换版本下的同名权重 /
    手工替换 / 缓存损坏）同样是"引擎变了"，只记名字查不出来。
    代价：一次性读 ~133MB（det 59 + rec 73 + cls 0.6），约 0.5s，每次运行只算一次。
    """
    try:
        import rapidocr
        from rapidocr.inference_engine.base import FileInfo, InferSession
        root = c.Global.get("model_root_dir") or (Path(rapidocr.__file__).parent / "models")
    except Exception:
        return {}
    out: Dict[str, str] = {}
    for sec in ("Det", "Cls", "Rec"):
        s = getattr(c, sec, None)
        if s is None:
            continue
        try:
            info = InferSession.get_model_url(FileInfo(
                engine_type=s.engine_type, ocr_version=s.ocr_version,
                task_type=s.task_type, lang_type=s.lang_type, model_type=s.model_type))
            p = Path(root) / Path(info["model_dir"]).name
            out[sec.lower()] = file_sha256(p) if p.is_file() else "?"
        except Exception:
            out[sec.lower()] = "?"
    return out


def _fingerprint_from_cfg_obj(c) -> Dict[str, Any]:
    """从 rapidocr 的 cfg 对象取引擎指纹（`engine_fingerprint` / `ocr_fingerprint` 共用）。"""
    return {
        "det": f"{c.Det.ocr_version.value}/{c.Det.model_type.value}",
        "rec": f"{c.Rec.ocr_version.value}/{c.Rec.model_type.value}",
        "cls": f"{c.Cls.ocr_version.value}/{c.Cls.model_type.value}",
        "backend": c.Det.engine_type.value,
        "max_side_len": c.Global.max_side_len,
        "min_side_len": c.Global.min_side_len,
        "limit_side_len": int(c.Det.limit_side_len),
        "limit_type": str(c.Det.limit_type),
        "thresh": round(float(c.Det.thresh), 6),
        "box_thresh": round(float(c.Det.box_thresh), 6),
        "unclip_ratio": round(float(c.Det.unclip_ratio), 6),
        "score_mode": str(c.Det.score_mode),
        "use_dilation": bool(c.Det.use_dilation),
        "text_score": round(float(c.Global.text_score), 6),
        "use_cls": bool(c.Global.use_cls),
        "use_vertical_padding": bool(c.Global.use_vertical_padding),
        "rec_batch_num": int(c.Rec.rec_batch_num),
    }


def engine_fingerprint(engine) -> Dict[str, Any]:
    """把当前引擎的配置抓成指纹，**用于写进产物做溯源**（需要已有引擎对象）。

    ⚠️ 想**不构造引擎**就要这个指纹（比如产物复用判据的预检阶段）用
    `ocr_fingerprint(cfg_path)` —— 它从配置文件读同一批键。
    """
    return {**_versions(), **_fingerprint_from_cfg_obj(engine.cfg),
            "model_sha256": _model_files(engine.cfg)}


def ocr_fingerprint(cfg_path: Optional[Union[str, Path]] = None, alts: bool = True,
                    top2_floor: Optional[float] = None,
                    top2_decimals: Optional[int] = None,
                    backend: str = "auto",
                    det_boxes_source: str = "raw",
                    hook_schema: int = 1) -> Dict[str, Any]:
    """**不构造引擎**推导「OCR + 产物口径」指纹（读配置文件解析）。

    用途：`pipeline.ocr` 的产物复用判据。产物只该在两种情况下重跑：
    ① 页图变了；② **口径变了**。

    「口径」必须覆盖**所有会影响产物内容或判据的东西**：
      · 运行时：`rapidocr` / `openvino` / `numpy` / `opencv` 版本
      · 后端：`backend`（**含 CLI 覆盖**）+ `backend_config` + `backend_override`
      · 模型：det / rec / cls 档位 + **权重文件实际 SHA256**
      · 配置：`max_side_len` / `box_thresh` / `text_score` / `use_cls` …
      · 本模块口径：`product_schema` / `hook_schema` / `alt_capture` /
        `top2_floor` / `top2_decimals` / `det_boxes_source`

    ⚠️ 历史坑一：判据原来只比页图 —— 于是**改完采集口径，旧产物照样被判"可复用"**
    （实测：旧口径产物缺 `top2`/`dropped_lines`/`top2_floor`，仍报"全部可复用"）。

    ⚠️ 历史坑二：`--backend` 原来**没有进指纹** —— 配置里写 openvino，同一条命令加
    `--backend onnxruntime` 仍被判"可复用"，产物无法回答"这页到底是哪个后端跑的"。
    现在三个键分开记，审计时能区分"配置要求什么"和"本次实际用了什么"：
      · `backend_config`   —— 配置文件里写的（本次没读配置时为 None）
      · `backend_override` —— CLI 原值（`auto` 表示没覆盖）
      · `backend`          —— **本次实际生效**的（override 生效后）
    比较只认 `backend`，但另外两个留着让 provenance 自解释。

    ⚠️ 必须能在**没有引擎**时算出来：预检阶段若比不了，就会出现"预检说全部可复用 →
    直接 return 0"，根本没机会比引擎参数。

    ⚠️ `det_boxes_source` 也进指纹：`raw` 与 `filtered` 的产物**审计能力不同**，
    不能互相复用（filtered 的产物会让漏行审计结构性失明）。
    """
    c = cfg_path or default_config_path()
    override = (backend or "auto").strip().lower()
    out: Dict[str, Any] = {"product_schema": PRODUCT_SCHEMA,
                           "hook_schema": int(hook_schema),
                           "alt_capture": bool(alts),
                           "top2_floor": top2_floor if alts else None,
                           "top2_decimals": top2_decimals if alts else None,
                           "det_boxes_source": det_boxes_source,
                           "backend_config": None,
                           "backend_override": override,
                           "backend": None}
    out.update(_versions())
    if override not in ("auto", "", "default"):
        out["backend"] = override
    if not c:
        out["_cfg_error"] = f"找不到配置文件（{DEFAULT_CONFIG_REL}）"
        return out
    try:
        from rapidocr.utils.parse_parameters import ParseParams
        cfg_obj = ParseParams().load(str(c))
        out.update(_fingerprint_from_cfg_obj(cfg_obj))
        out["model_sha256"] = _model_files(cfg_obj)
        out["backend_config"] = out["backend"]          # 配置里写的
        if override not in ("auto", "", "default"):
            out["backend"] = override                   # 实际生效的
    except Exception as e:
        # 解析不出来**绝不猜参数**：猜出来的指纹会让复用判据给出错误结论
        # （"看着一致"其实没比到）。这里只如实标记 `_cfg_error`；
        # **由 `fingerprint_drift()` 保证它一定被算作"不可复用"** ——
        # 标记一个 `_` 前缀的错误键然后让人忽略它，等于没有 fail closed。
        out["_cfg_error"] = f"{type(e).__name__}: {e}"
    return out


def fingerprint_drift(stored, want: Dict[str, Any]) -> List[str]:
    """逐键比对指纹，返回差异说明（空列表 = 一致）。只比 `want` 里出现的键。

    🔴 **fail closed**：`want` 里只要有 `_` 前缀的键（如 `_cfg_error`），一律判为
    "不一致"，并把它当成差异原因返回。

    为什么必须这样：原来这些 `_` 键在比对时被**跳过**，于是"指纹算不出来"会退化成
    "指纹一致" —— 缓存命中一个**从来没比过**的口径，旧产物被当成新的继续往下跑。
    这比"多比了一轮"危险得多：它让整个过期守卫静默失效。
    ⇒ 只返回非空的比对结果，调用方不可能把它误读成"可以复用"。
    """
    if not isinstance(want, dict):
        return ["指纹不是字典（内部 bug）"]
    errs = [(k, v) for k, v in want.items() if str(k).startswith("_")]
    if errs:
        detail = "；".join(f"{k}: {v}" for k, v in errs)
        return [f"指纹无法计算 → 一律判为不可复用（{detail}）"]
    stored = stored or {}
    out: List[str] = []
    for k, v in want.items():
        got = stored.get(k, "<缺>")
        if got != v:
            out.append(f"{k}: {got!r} → {v!r}")
    return out


# ---------------------------------------------------------------------------
# ⑧ 输出目录独占锁（清单 / 汇总 JSON 都是 read-modify-write）
# ---------------------------------------------------------------------------
class OutputLock:
    """输出目录的独占锁。

    凡是"读旧清单 → 合并 → 整体写回"的地方都需要它：两个进程同时跑，各自读到旧值、
    各自写回，**后写的会静默覆盖前者的更新**（磁盘上 100 条记录、清单里只剩 50 条）。

    锁机制是 OS 级 advisory lock（Windows `msvcrt` / POSIX `fcntl`）：**进程退出时
    由内核释放**，包括崩溃和被强杀 —— 所以不会留下"僵尸锁"。锁文件本身留着无害。
    """

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
        fd = os.open(str(self.path), flags)
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# ⑨ 杂项
# ---------------------------------------------------------------------------
def inc(stat: Dict[str, int], key: str, n: int = 1) -> None:
    """计数式记账（**唯一实现**，各阶段共用）。

    存在的意义：本项目的头号失败模式是"声明与实现不一致且不报错"。
    凡是要写进日志/报告的那句"改了几条 / 一致 / 为空"，都必须来自一个**显式的数**，
    而不是靠 `print` 里临时算的表达式 —— 那句 print 会被 `tail` 切掉。
    """
    stat[key] = stat.get(key, 0) + n


def force_utf8_stdout() -> None:
    """Windows 控制台默认 GBK，中文日志会炸/乱码。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def list_pages(pages_dir: Union[str, Path], pattern: str = "page_*.png") -> List[str]:
    """列出页名（不含扩展名），按字典序（page_001 ... page_316）。"""
    d = Path(pages_dir)
    return sorted(p.stem for p in d.glob(pattern))


# ---------------------------------------------------------------------------
# ⑩ 证据归一化：读产物的**唯一**口径
# ---------------------------------------------------------------------------
def iter_alts(line: Dict[str, Any]):
    """把两种产物口径归一成 `{"i","t","c1","p1","c2","p2"}`，按字符位置升序。

    · **`top2`（现口径，带采集下限）**：`[[i,t,p1,c2,p2], ...]`，
      `i` = 字符在行内的下标，`c1` 即 `text[i]`（所以不重复存）。
      **只记 `p2/p1 >= provenance.top2_floor` 的位置**，所以数组**不与 `text` 对齐** ——
      定位一律用 `i`，别用数组位置。
    · **`alt_candidates`（旧口径，带门槛）**：`[{"i","t","c1","p1","c2","p2"}, ...]`，
      只记 `p2/p1 > 0.05 且 p2 > 0.02` 的"摇摆位置"。

    必须同时支持两者：存量产物里可能有旧口径，不该因为它老就重跑。

    ⚠️ 混用时的可比性：旧产物的比值**天然不存在 0.05 以下**，拿它扫更低的阈值
    会得到"结果相同"，那是**同义反复**不是结论。

    🔴 **所有 OCR 证据关联都应依赖稳定 ID / index，而不是文本** ——
    `top2` 用 `i` 定位、`det_boxes` 用 `det_index` 关联，都是同一条原则。
    """
    txt = line.get("text") or ""
    top2 = line.get("top2")
    if top2:
        for e in top2:
            if not isinstance(e, (list, tuple)) or len(e) < 5:
                continue
            i = int(e[0])
            if not (0 <= i < len(txt)):
                continue
            yield {"i": i, "t": e[1], "c1": txt[i], "p1": e[2], "c2": e[3], "p2": e[4]}
        return
    for a in line.get("alt_candidates") or []:
        if isinstance(a, dict):
            yield a


# ---------------------------------------------------------------------------
# ⑪ 字级水平定位：「这个字在原图哪儿」的唯一实现
# ---------------------------------------------------------------------------
# 背景（实测，别重新推一遍）：
#   `det` 只给**整行**框，没有逐字框。此前所有"按字裁片"的地方都用
#   「**字数均分**」估字符 x —— 等宽假设，行内一混标点/拉丁/数字就错位，
#   而 `人/入`、`已/己` 的差别恰恰在**只看像素**才判得出来。
#
#   其实模型自己给了逐字水平位置：CTC 的**时间步**。实测 `padW / T == 8.00`
#   （归一化图上每个时间步 = 8 px），所以 t 与 x 成正比。
#   rapidocr 也**原生支持**把它变成字符框（`cal_rec_boxes`），
#   只要 `Global.return_word_box: True`：`TextRecOutput.word_results[0]` 是 `WordInfo`，
#   带 `word_cols`（逐字时间步）与 `line_txt_len`（内容占用的**时间步总数**）。
#
#   🔴 库自己的换算是 `x_center = (col + 0.5) * W / line_txt_len`
#      （`cal_rec_boxes/main.py::calc_box`）。实测 `line_txt_len` 与理论 `6 * W / H`
#      逐条吻合，说明它是**归一化后的时间步**，不需要我们再复制 rapidocr 的预处理公式。
#
#   🔴 两个坑（都实测踩到）：
#      ① `word_cols` **不含空白字符** —— 库分组时 `if char.isspace(): continue`。
#         按数组位置对齐会**整体错位**，必须按文本逐字对齐（本模块负责补齐空白位）。
#      ② rec_only 路径下 `Global.use_preprocess_img` 默认 **True**，会把送进来的
#         裁片缩到 ≤2000 并按 **32 取整**（实测 2442×93 → 1984×64，行高被压 31%）。
#         做字级定位时必须关，否则 x 的比例被扭。

#: 时间步 → 像素的比例（归一化图 48px 高时）。**只用于自检**，正常路径不依赖它。
CTC_PX_PER_STEP = 8.0


def rec_line_word_cols(
    engine, img: np.ndarray
) -> Tuple[str, Optional[float], Optional[List[float]], Optional[float]]:
    """单行 rec-only，返回 `(text, conf, cols, line_txt_len)`。

    `cols` 与 `text` **逐字符 1:1 对齐**（空白位由相邻列线性插值补上，
    因为库的 `word_cols` 丢掉了空白 —— 见本节顶部坑 ①）。

    取不到（未开 `return_word_box`、或对齐失败）时 `cols=None`，调用方应退回均分。

    ⚠️ **本函数不吞异常** —— 引擎抛错就让它冒出去，由调用方**记账**。
       本项目栽过这个坑：`except` 只 `print` 不留计数，日志被 `tail` 切掉后
       表现就是"这些位置没有候选"，看起来完全正常。**响亮失败优于静默降级。**

    ⚠️ 取值一律用**显式长度判断**，不要用 truthiness：`res.txts` 若哪天变成
       numpy 数组，`not arr` 会抛 `ValueError: truth value ... is ambiguous`。
       这类"依赖返回类型恰好是 list"的写法在库升级时会变成难查的崩溃。

    前提：`engine` 必须是
    `build_engine(rec_only=True, extra_params={"Global.use_preprocess_img": False,
    "Global.return_word_box": True, "Global.return_single_char_box": True})`
    —— 否则裁片会被预处理扭曲，`line_txt_len` 也就不再对应**你给的那张图**。
    """
    res = engine(img)
    if res is None:
        return "", None, None, None
    txts = getattr(res, "txts", None)
    if txts is None or len(txts) == 0:
        return "", None, None, None
    text = txts[0] or ""
    scores = getattr(res, "scores", None)
    conf = float(scores[0]) if (scores is not None and len(scores) > 0) else None
    wrs = getattr(res, "word_results", None)
    wi = wrs[0] if (wrs is not None and len(wrs) > 0) else None
    if wi is None or not text:
        return text, conf, None, None
    raw = [int(c) for wc in (getattr(wi, "word_cols", None) or ()) for c in wc]
    L = getattr(wi, "line_txt_len", None)
    if L is None or not raw:
        return text, conf, None, None

    n_nonspace = sum(0 if ch.isspace() else 1 for ch in text)
    if n_nonspace != len(raw):
        # 对齐失败比给错位置好：**响亮地退回**均分，并把原因交给调用方打印。
        return text, conf, None, float(L)

    cols: List[float] = []
    k = 0
    i = 0
    while i < len(text):
        if not text[i].isspace():
            cols.append(float(raw[k]))
            k += 1
            i += 1
            continue
        j = i
        while j < len(text) and text[j].isspace():
            j += 1
        prev = cols[-1] if cols else 0.0
        nxt = float(raw[k]) if k < len(raw) else prev + (j - i)
        span = j - i
        for m in range(1, span + 1):
            cols.append(prev + (nxt - prev) * m / (span + 1))
        i = j
    return text, conf, cols, float(L)


def char_x_spans_from_cols(
    bbox: Sequence[float], cols: Sequence[float], line_txt_len: Optional[float] = None
) -> Optional[List[Tuple[float, float]]]:
    """逐字水平区间（原图坐标）—— **端点标定**口径，本项目实际使用的那一个。

    🔴 **这是"中心定位"模型，不是"真实字符 bbox"模型**（别误解）：
    每个字符的宽度**一律取均分宽 `cw = W / n`**，只有**中心点**按 CTC 时间步伸缩：

        x_center(i) = x0 + cw/2 + (W - cw) * (col_i − col_first) / (col_last − col_first)

    然后由相邻中心点的**中点**切出边界，首尾各外延 `cw/2`。
    也就是说它给出的是"**该字最可能落在哪一段**"，用于**裁片取像素**足够准，
    但**不等于** OCR 模型输出的真实字符边界（宽字/窄字仍被当成等宽）。

    🔴 为什么不用库自己的绝对换算 `(col + 0.5) * W / line_txt_len`
    （即 `char_x_spans_from_cols_raw`）：实测在本项目台账上有**系统性偏移** ——
    用「边界切墨量」这个客观代理量比较，raw 口径中位 **0.127**，而字数均分 **0.056**，
    即 raw 的边界系统性压在笔画上。偏移量约 10~15 px（≈ 一个时间步）。
    端点标定把这个恒定偏移**整体消掉**（首尾都被钉在框上），同时**保留**字距不均的信息
    —— 对等距的行，它与均分**数学上恒等**。

    ⇒ 三种口径的关系：
       `even`（均分）            假设**所有**字等宽且铺满全框
       `raw`（库绝对换算）        绝对位置准，但有 ~0.15 字宽的恒定偏移
       `from_cols`（端点标定）    ← 本函数：无偏移，且随真实字距伸缩

    `line_txt_len` 只用于**自洽检查**（raw 口径要用），端点标定不依赖它。
    """
    n = len(cols)
    if n <= 0 or len(bbox) < 4:
        return None
    x0, _, w, _ = (float(v) for v in bbox[:4])
    if w <= 0:
        return None
    cw = w / n
    if n == 1:
        return [(x0, x0 + w)]
    span = float(cols[-1]) - float(cols[0])
    if span <= 0:
        return None
    centers = [x0 + cw / 2.0 + (w - cw) * (float(c) - float(cols[0])) / span for c in cols]
    if any(centers[i] > centers[i + 1] for i in range(n - 1)):
        return None                       # 非单调 = 口径有问题，别拿去裁图
    edges = ([centers[0] - cw / 2.0]
             + [(centers[i] + centers[i + 1]) / 2.0 for i in range(n - 1)]
             + [centers[-1] + cw / 2.0])
    return list(zip(edges[:-1], edges[1:]))


def char_x_spans_from_cols_raw(
    bbox: Sequence[float], cols: Sequence[float], line_txt_len: float
) -> Optional[List[Tuple[float, float]]]:
    """**库原口径**：`x_center = (col + 0.5) * W / line_txt_len`（`cal_rec_boxes::calc_box`）。

    保留它只为可复现的 A/B（`eval/review_sheet.py --loc raw`）—— 实测有 ~0.15 字宽的
    恒定偏移，**不要**拿它当默认。见 `char_x_spans_from_cols` 的说明。
    """
    n = len(cols)
    if n <= 0 or not line_txt_len or line_txt_len <= 0 or len(bbox) < 4:
        return None
    x0, _, w, _ = (float(v) for v in bbox[:4])
    if w <= 0:
        return None
    centers = [x0 + w * (float(c) + 0.5) / float(line_txt_len) for c in cols]
    if any(centers[i] > centers[i + 1] for i in range(n - 1)):
        return None
    cw = w / n
    edges = ([centers[0] - cw / 2.0]
             + [(centers[i] + centers[i + 1]) / 2.0 for i in range(n - 1)]
             + [centers[-1] + cw / 2.0])
    return list(zip(edges[:-1], edges[1:]))


def char_x_spans_even(bbox: Sequence[float], n: int) -> Optional[List[Tuple[float, float]]]:
    """「**字数均分**」估的逐字区间 —— 无 `cols` 时的退路，也是对照基线。

    ⚠️ 只对**等宽、且铺满整框**的行成立。混了标点/拉丁/数字就会错位，
    这正是它被替换掉的原因。保留它是为了 `--loc even` 能随时做 A/B。
    """
    if n <= 0 or len(bbox) < 4:
        return None
    x0, _, w, _ = (float(v) for v in bbox[:4])
    if w <= 0:
        return None
    return [(x0 + w * i / n, x0 + w * (i + 1) / n) for i in range(n)]
