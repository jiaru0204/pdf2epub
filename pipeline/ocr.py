# -*- coding: utf-8 -*-
"""OCR 主链路：页图 → 逐页文本行产物（`*.textlines.json`，含 `det_boxes`）。

产线第二环。它是「漏行审计」的**数据来源**：审计阶段自己不跑 det，
只读产物里的 `det_boxes` 字段 —— 所以本阶段**必须**把这个字段落盘，
否则审计会整本页页响亮跳过（`no_det_boxes`）。

用法
----
    python -m pipeline.ocr --pages <页图目录> [--out <产物目录>]
    python -m pipeline.ocr --pages pages --out textlines            # 全书
    python -m pipeline.ocr --pages pages --pages-filter page_099    # 定点复跑
    python -m pipeline.ocr --pages pages --limit 5 --dry-run        # 先预览
    python -m pipeline.ocr --pages pages --max-minutes 9            # 分批（可续跑）

产物格式
--------
```json
{
  "page": "page_099",
  "engine": "rapidocr-3.9.2/openvino/PP-OCRv6-medium",
  "line_count": 25,
  "elapsed_sec": 5.8,
  "provenance": { "backend": "openvino", "det": "PP-OCRv6/medium",
                  "det_boxes_source": "raw", "audit_capable": true,
                  "alignment": "det_index", "product_schema": 5, ... },
  "lines": [ {"line_id":1,"orig_line_id":1,"added":false,"det_index":0,
              "bbox":[x,y,w,h],"text":"我们自己…","confidence":0.998,
              "top2":[[37,0.907,"已",0.093], [38,0.999,"们",0.0004]]} ],
  "det_boxes": [[x,y,w,h], ...],
  "dropped_lines": [ {"det_index":3, "bbox":[...], "text":"", "score":0.0,
                      "reason":"empty_text", "wh":3.1, "top2":[...]} ]
}
```

`det_boxes` = **过滤前的原始 det 框**（原图坐标，`[x,y,w,h]`）。它与产物行
**不必然 1:1** —— 差值就是"有框无行"的漏行候选，这正是审计要找的东西。

🔴 **`det_index` 是证据归属的唯一依据**（本模块最重要的一条口径）
----------------------------------------------------------------
`det_boxes[k]` 与 `lines[].det_index == k`、`dropped_lines[].det_index == k`
指的是**同一个 det 框**。这条链路是：

    det 框顺序  →  crop 顺序（`crop_text_regions` 按框顺序裁）  →  rec 输入顺序
      →  rec 内部按宽高比重排分批（`np.argsort(width_list)`）  →  解码结果
      →  按 **det 下标** 归位

早期版本靠"**文本多重集**"把 rec 捕获结果反查回产物行 —— 同一页出现两次
「你好」时，谁挂到哪一行只能靠 `bucket.pop(0)` 猜。对形近字分析是致命的：
那是**把 A 行的模型证据挂到 B 行的框上**。现在改为在 `TextRecognizer.__call__`
处**复现引擎自己的 argsort 分批**，拿到每个批次位置的 det 下标 —— 零额外推理、
零猜测，且每页做一次 `len(kept) == len(lines)` 的对账，对不上就**响亮失败**
（该页 top2 记空并计入 `top2_errors`），绝不退回文本匹配。

⚠️ `--allow-filtered-det` 降级路径：拿不到原始框时 `det_boxes` 回落成过滤后的框，
`det_index` 无法再对应 `det_boxes`（仅对应 crop 顺序），因此
`provenance.audit_capable=False`，且产物**不应**再被审计消费。

⚠️ 为什么不能用 `result.boxes` 当原始框（这条极易踩错）
------------------------------------------------
rapidocr 的 `RapidOCR.__call__` 返回前做**两道过滤**（空文本过滤、`text_score` 过滤），
而且两道都是**框与文本一起删**。所以 `result.boxes` 与 `result.txts` **永远 1:1** ——
拿它当 `det_boxes` 会让审计永远报 0 差异，它要找的"有框无行"在数据里根本无法存在。

正确做法是挂钩 `rapidocr.main.map_boxes_to_original`：它恰好在 det 之后、
两道过滤之前执行，拿到的就是**原始框**（已映射回原图坐标）。

`top2`（`--alts` 默认开）= 每行的 **top-1/top-2 候选**，每条 `[i, t, p1, c2, p2]`：
`i` = 字符在**行内的下标**、`t` = CTC 时间步。**带采集下限**（`TOP2_FLOOR`，
写在 `provenance.top2_floor`）：低于它的位置不落盘。⚠️ 因此数组**不与 `text` 逐字对齐**，
定位一律用 `i`。

`dropped_lines` = **det 有框、产物无行**的那些框，rec 当时到底说了什么
（`text` / `score` / `top2` / `reason`）。`reason` 只有三种取值：
`empty_text`（rec 输出了空文本）/ `low_score`（有文本但 < `text_score`）/
`unresolved`（这一框没有可信的 rec 行）。`bbox` 直接取自 `det_boxes[det_index]`，
**不做任何二次匹配**。

设计要点（沿用本项目既有约束）
------------------------------
1. **`det_boxes` 字段必须存在**：空列表（空白页/纯图页）与字段缺失是两回事 ——
   审计靠这个区分「这页本来没框」和「生产者没接上」。
2. **原子写盘 + 紧凑序列化**：统一走 `linefix.common.write_product_json`。
3. **断点续跑 + 过期守卫**：已有产物只有在**能解析 + 页名一致 + 页图未变 + 口径未变**
   时才复用（判据只写在 `check_reusable()` 一份，预检与主循环共用）。
4. **时间预算**：`--max-minutes` 到点干净退出（exit 3，可续跑）。
   ⚠️ 已知边界：预算只在**页间**检查，单页若卡死仍会拖住整批（Windows 没有
   `signal.alarm`）。宁可让它卡住，也不做"猜着超时"的静默降级。
5. **埋点失败一律响亮**：这是审计证据，不是可选的 debug 输出。
   钩子装不上 → 直接退出（要降级请显式 `--no-alts`）；埋点抛异常 → 进
   `provenance.top2_error` 并计入汇总的 `top2_errors`，绝不让产物"安静地写空"。

⚠️ 本模块**绝不**调用 `engine(..., use_rec=False)`。RapidOCR(OpenVINO) 的
   call-time `use_rec=False` 会**永久污染该实例**的后续 rec（静默返回 0 行，不报错）。
   本模块用的钩子都是**同一次前向**的副产品，不额外推理、不改变实例状态。
"""
from __future__ import annotations

import argparse
import importlib.metadata as md
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

# 允许从任意工作目录运行（`python -m pipeline.ocr` 通常在项目根，但不强制）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from linefix import common as C  # noqa: E402

MANIFEST = "ocr_manifest.json"
LOCKFILE = ".ocr.lock"
LOW_CONF = 0.75          # 与下游救援阶段的 W2 阈值同源
TOP2_DECIMALS = 4        # 概率保留位数（3 位会让"贴着阈值"的位置分辨率不足）

# `top2` 的采集下限（按 `p2/p1` 比）—— **必须严格低于任何打算评估的阈值**。
#
# 实测（6 页 / 2557 字）比值分布：
#   恰好 = 0（p2 < 5e-5，纯噪声） 60.8% ｜[1e-4,1e-3) 31.4%
#   [1e-3,0.01) 5.3% ｜[0.01,0.05) 0.8% ｜≥0.05 1.7%
# 取 1e-3：砍掉的 92% 绝大多数是"恰好 0"（第 2 名是字符表里 18710 个类中随便挑的），
# 有信息量的候选（>1e-3）只占 7.8%，全部保留；给产线规则阈值 0.05 留 50× 余量。
# 代价（务必记住）：**再往下扫（如 5e-4）就是空白**。
# 历史教训：早先门槛设成 0.05（产线阈值本身），于是"阈值扫到 0.03 结果不变"被
#   当成"下限稳定"的结论，其实 0.05 以下根本没采。**采集必须比评估更宽。**
TOP2_FLOOR = 1e-3

# ---------------------------------------------------------------------------
# 🔴 兼容层版本闸门
# ---------------------------------------------------------------------------
# 本模块 hook 的是 rapidocr 的**内部**结构（`map_boxes_to_original` /
# `TextRecognizer.__call__` 的排序分批 / `CTCLabelDecode.__call__`）。
# 这些都不是稳定的公开 API：一旦改名或换实现，后果不是"结果略变"，而是
# **审计证据直接消失**。所以只允许在实测过的版本上跑，未验证版本**拒绝生产**。
SUPPORTED_RAPIDOCR = ("3.9.2",)

# hook 集合 / 归位口径变更时 +1。写进产物，让"两代 hook 口径的产物混用"响亮。
HOOK_SCHEMA = 1

_RAW: Dict[str, Any] = {"boxes": None}      # 原始 det 框（过滤前，原图坐标）
_ALTS: Dict[str, Any] = {"on": False, "cap": [], "cap_errors": 0, "last_error": None}
_REC: Dict[str, Any] = {"batches": [], "i": 0, "plan_error": None}


# ---------------------------------------------------------------------------
# 埋点一：原始 det 框（过滤前）
# ---------------------------------------------------------------------------
def install_raw_det_hook() -> bool:
    """包一层 `rapidocr.main.map_boxes_to_original`，捕获过滤前的 det 框。

    该函数在 det 之后、空文本/分数两道过滤**之前**执行，所以拿到的框
    与 `result.boxes`（过滤后）不是一回事 —— 详见模块 docstring。
    """
    try:
        import rapidocr.main as rm
    except Exception as e:                                    # pragma: no cover
        print(f"[raw-det] 无法导入 rapidocr.main：{e}")
        return False
    if not hasattr(rm, "map_boxes_to_original"):
        print("[raw-det] rapidocr.main 里没有 map_boxes_to_original "
              "（版本可能已变，过滤前的框拿不到）")
        return False
    if getattr(rm.map_boxes_to_original, "_pipeline_raw_det", False):
        return True

    orig = rm.map_boxes_to_original

    def hooked(boxes, *a, **kw):
        out = orig(boxes, *a, **kw)
        try:
            _RAW["boxes"] = None if out is None else np.array(out, dtype=float, copy=True)
        except Exception:
            _RAW["boxes"] = None
        return out

    hooked._pipeline_raw_det = True
    rm.map_boxes_to_original = hooked
    return True


def _xywh_quads(quads) -> List[list]:
    """四点框 → `[x, y, w, h]`（整数）。几何口径复用 `linefix.common.xywh`。"""
    out: List[list] = []
    for box in quads:
        x, y, w, h = C.xywh(box)
        out.append([round(x), round(y), round(w), round(h)])
    return out


# ---------------------------------------------------------------------------
# 埋点二：rec 的「批次位置 → det 下标」映射
# ---------------------------------------------------------------------------
def _plan_rec_batches(engine, args) -> List[List[int]]:
    """复现 `TextRecognizer.__call__` 自己的排序与分批，返回每批的 det 下标。

    引擎内部（`ch_ppocr_rec/main.py`）是：

        width_list = [img.shape[1] / img.shape[0] for img in img_list]
        indices = np.argsort(np.array(width_list))
        for beg in range(0, img_num, batch_num):
            ...  postprocess_op(preds)              # 每批调一次
            rec_res[indices[beg + rno]] = ...

    `img_list` 就是 det 框顺序裁出来的 crop 列表（`crop_text_regions` 按框顺序裁），
    所以 `indices[k]` **就是** det 下标。我们原样重算一遍即可 ——
    同样的数组进 `np.argsort` 得到同样的结果，不需要额外推理、也不改变引擎行为。
    """
    img_list = [args.img] if isinstance(args.img, np.ndarray) else list(args.img)
    width_list = [img.shape[1] / float(img.shape[0]) for img in img_list]
    indices = np.argsort(np.array(width_list))
    batch_num = int(engine.rec_batch_num)
    out: List[List[int]] = []
    for beg in range(0, len(img_list), batch_num):
        end = min(len(img_list), beg + batch_num)
        out.append([int(indices[i]) for i in range(beg, end)])
    return out


def install_rec_index_hook() -> bool:
    """包一层 `TextRecognizer.__call__`，把每批的 det 下标映射排好队。

    与 `install_alts_hook` 配合：一次 `TextRecognizer.__call__` 会按批调用
    `CTCLabelDecode.__call__`，我们把批次映射排成 FIFO，由后者按顺序取走。
    """
    try:
        from rapidocr.ch_ppocr_rec.main import TextRecognizer
    except Exception as e:                                    # pragma: no cover
        print(f"[rec-idx] 无法导入 TextRecognizer，下标归位不可用：{e}")
        return False
    if getattr(TextRecognizer.__call__, "_pipeline_rec_index", False):
        return True

    orig = TextRecognizer.__call__

    def patched(self, args):
        # ⚠️ `rec_batch_num` 是**实例**属性（在 __init__ 里设），类上取不到 ——
        #    所以不能靠类属性做能力探测，只能在这里实探：取不到就记 `plan_error`，
        #    随后由 `_collect_rec_rows` 的条数对账**响亮失败**（绝不退回文本匹配）。
        try:
            _REC["batches"] = _plan_rec_batches(self, args)
            _REC["plan_error"] = None
        except Exception as e:
            _REC["batches"] = []
            _REC["plan_error"] = f"{type(e).__name__}: {e}"
        _REC["i"] = 0
        return orig(self, args)

    patched._pipeline_rec_index = True
    TextRecognizer.__call__ = patched
    return True


# ---------------------------------------------------------------------------
# 埋点三：top2（每个解码字符的 top-1/top-2）
# ---------------------------------------------------------------------------
def install_alts_hook() -> bool:
    """给 `CTCLabelDecode.__call__` 打补丁。返回是否可用。

    一次调用 = 一个 rec 批次（`rec_batch_num` 条）。除了 top-2 概率，还顺手留下
    ① 该批的 `wh_ratio_list` ② `orig` 返回的 `(text, score)` 列表（用来做
    "文本重建是否与 rec 一致"的**自检**）③ 从 `_REC` 取走的 det 下标映射。
    """
    try:
        from rapidocr.ch_ppocr_rec.utils import CTCLabelDecode
    except Exception as e:                                    # pragma: no cover
        print(f"[top2] 无法导入 CTCLabelDecode，埋点关闭：{e}")
        return False
    if getattr(CTCLabelDecode.__call__, "_pipeline_alts", False):
        return True

    orig = CTCLabelDecode.__call__

    def patched(self, preds, return_word_box=False, **kw):
        out = orig(self, preds, return_word_box, **kw)        # 先走原逻辑，不改行为
        if _ALTS["on"]:
            try:
                # 取走本批次对应的 det 下标（由 install_rec_index_hook 排好队）。
                idx = _REC["i"]
                _REC["i"] = idx + 1
                mapping: List[int] = _REC["batches"][idx] if idx < len(_REC["batches"]) else []
                a = np.asarray(preds, dtype=np.float32)
                i1 = a.argmax(axis=2)                                  # [B,T]
                p1 = np.take_along_axis(a, i1[..., None], axis=2)[..., 0]
                a2 = a.copy()
                np.put_along_axis(a2, i1[..., None], np.float32(-1.0), axis=2)
                i2 = a2.argmax(axis=2)
                p2 = np.take_along_axis(a2, i2[..., None], axis=2)[..., 0]
                # ⚠️ 必须**立刻**转成小结构：长期持有 [B,T,18710] 大数组会让
                #    OpenVINO segfault。
                _ALTS["cap"].append(
                    (mapping,
                     i1.astype(np.int32), i2.astype(np.int32),
                     p1.astype(np.float32), p2.astype(np.float32),
                     list(kw.get("wh_ratio_list") or ()),
                     list(out[0]) if out else [])
                )
                del a, a2
            except Exception as e:                       # 埋点绝不影响 OCR 本体
                _ALTS["cap_errors"] = int(_ALTS.get("cap_errors", 0)) + 1
                _ALTS["cap_last_error"] = f"{type(e).__name__}: {e}"
                print(f"[top2] 捕获失败（第 {_ALTS['cap_errors']} 次，忽略）: "
                      f"{_ALTS['cap_last_error']}")
        return out

    patched._pipeline_alts = True
    CTCLabelDecode.__call__ = patched
    return True


def _top2_entries(p1_row, i2_row, p2_row, keep, chars) -> List[list]:
    """把一行里每个解码字符的 top-2 收成 `[i, t, p1, c2, p2]`（按采集下限过滤）。

    ⚠️ 要**三个**数组：`p1`（top-1 概率）、`i2`（top-2 的类下标）、`p2`（top-2 概率）。

    `i` = **该字在行内的字符下标**，`t` = CTC 时间步。`c1` 不存 —— 它就是 `text[i]`。

    ⚠️ 带下限过滤后数组**不再与 `text` 逐字对齐**（有些位置没有条目），所以 `i`
    必须显式存。**不要把"数组位置"当字符下标用**。
    """
    out: List[list] = []
    for ci, t in enumerate(keep):
        pa = float(p1_row[t])
        pb = float(p2_row[t])
        if pa > 0 and pb / pa >= TOP2_FLOOR:
            out.append([ci, int(t), round(pa, TOP2_DECIMALS),
                        chars[int(i2_row[t])], round(pb, TOP2_DECIMALS)])
    return out


def _collect_rec_rows(engine) -> Tuple[Dict[int, Dict[str, Any]], int]:
    """把本页所有 rec 批次的结果**按 det 下标**归位，返回 `{det_index: row}` 与
    "文本重建与 rec 实际输出不一致"的行数。

    🔴 对齐全靠 det 下标，**不做任何文本匹配**。同一页出现两条完全相同的文本时，
    文本匹配只能靠 `pop(0)` 猜；下标则是确定的。

    ⚠️ 批次映射条数与实际解码行数不符 → **直接抛错**。宁可这一页 top2 记空
    （并计入 `top2_errors`），也不退回"猜"的匹配方式：那等于把 A 行的模型证据
    挂到 B 行的框上。

    ⚠️ 字符表取不到也**不吞**：由 `ocr_one` 那个唯一记账处报出来。
    """
    cap = _ALTS["cap"]
    if not cap:
        return {}, 0
    chars = engine.text_rec.postprocess_op.character

    by_det: Dict[int, Dict[str, Any]] = {}
    n_mismatch = 0
    for mapping, i1, i2, p1, p2, whs, outs in cap:
        if len(mapping) != int(i1.shape[0]):
            raise RuntimeError(
                f"rec 批次下标对不上：复现的映射 {len(mapping)} 条 vs 本批解码 "
                f"{int(i1.shape[0])} 行（rapidocr 内部排序/分批可能已变）")
        for bi in range(i1.shape[0]):                # 逐行对齐，**不能** concatenate
            det_i = int(mapping[bi])
            seq = i1[bi]                             # （rec 分批，各批 T 不同）
            sel = np.ones(len(seq), bool)
            sel[1:] = seq[1:] != seq[:-1]            # CTC 去重
            sel &= seq != 0                          # 去掉 blank
            keep = np.where(sel)[0]
            txt = "".join(chars[int(j)] for j in seq[keep])
            # rec 自己算出的文本才是权威。不一致时下标不能信 → 该行标 unresolved。
            if bi < len(outs) and outs[bi][0] != txt:
                n_mismatch += 1
                continue
            by_det[det_i] = {
                "text": txt,
                "score": round(float(outs[bi][1]), 3) if bi < len(outs) else None,
                "wh": round(float(whs[bi]), 3) if bi < len(whs) else None,
                "entries": _top2_entries(p1[bi], i2[bi], p2[bi], keep, chars),
            }
    return by_det, n_mismatch


def _assemble(lines: List[Dict[str, Any]], det_boxes: List[list],
              raw_quads: Optional[List[list]], engine,
              n_expected_lines: int) -> Dict[str, Any]:
    """按 det 下标把 top2 挂到行上，并交出**被丢弃的行**（含精确 bbox）。

    返回 stats：
        top2_entries / dropped / mismatch / error / cap_errors / no_index
        alignment ∈ {"det_index", "empty", "off"}；抛异常由调用方记成 "failed"

    ⚠️ `dropped` 是**直接推导**出来的（每个 det 下标要么进 kept、要么进 dropped），
    不再用 `len(det_boxes) - len(lines)` 反推。那个减法只是后面的一道**交叉校验**
    —— 它隐含"1 框 = 1 rec = 1 行"，一旦引擎内部 pipeline 变了就不成立。
    """
    base: Dict[str, Any] = {
        "top2_entries": 0, "dropped": [], "mismatch": 0, "error": None,
        "cap_errors": int(_ALTS.get("cap_errors", 0)), "no_index": 0,
        "alignment": "off",
    }
    n_crops = sum(len(b) for b in _REC["batches"]) if _REC["batches"] else 0
    cap = _ALTS["cap"]
    if not cap:
        # 空白页/纯图页：本来就没有 crop 要归位，这是"无事可做"不是"没对齐"。
        if n_crops == 0:
            return {**base, "alignment": "empty"}
        # 有 crop 却一条都没捕获到 —— 说明 rec 侧钩子没生效。**响亮失败**，
        # 否则会表现成"这页没有任何候选"，与"这页确实很确定"无法区分。
        raise RuntimeError(
            f"本页有 {n_crops} 个 crop 需要归位，但 rec 捕获为空"
            f"（{_REC.get('plan_error') or 'CTCLabelDecode 钩子未生效'}）")

    by_det, n_mismatch = _collect_rec_rows(engine)

    # det 框总数：优先用原始框；钩子失效时用"复现的批次映射总长"（= crop 数），
    # 它比"过滤后的框数"更接近真相（过滤后的框数只等于行数）。
    if raw_quads is not None:
        n_det = len(raw_quads)
    elif n_crops:
        n_det = n_crops
    else:
        n_det = len(det_boxes)

    thr = float(getattr(engine, "text_score", 0.0) or 0.0)

    def _bbox(k: int) -> Optional[list]:
        return raw_quads[k] if raw_quads is not None and k < len(raw_quads) else None

    kept: List[int] = []
    dropped: List[Dict[str, Any]] = []
    for k in range(n_det):
        r = by_det.get(k)
        if r is None:
            # 没有可信 rec 行的框（捕获失败 / 文本重建不一致）。**必须单独成类** ——
            # 否则会和"rec 明确输出了空文本"混在一起，两者含义完全不同。
            dropped.append({"det_index": k, "bbox": _bbox(k), "text": None,
                            "score": None, "wh": None, "top2": None,
                            "reason": "unresolved"})
            continue
        text = r["text"] or ""
        if text.strip() and float(r["score"] or 0.0) >= thr:
            kept.append(k)
            continue
        dropped.append({"det_index": k, "bbox": _bbox(k), "text": r["text"],
                        "score": r["score"], "wh": r["wh"], "top2": r["entries"],
                        "reason": "empty_text" if not text.strip() else "low_score"})

    # 🔴 对账：按 det 下标算出来的行必须与实际产物行 1:1。对不上说明下标链路
    #    （argsort 复现 / 两道过滤 / 产物行构造）某一环已经变了，此时**任何**
    #    top2 归属都不可信 → 抛错，让这一页记成 top2_error。
    if len(kept) != n_expected_lines:
        raise RuntimeError(
            f"det 下标对齐与产物行数不符：按下标算得 {len(kept)} 行，"
            f"产物实际 {n_expected_lines} 行（det 框 {n_det} 个）")

    n_chars = 0
    for m, det_i in enumerate(kept):
        entries = by_det[det_i]["entries"]
        lines[m]["top2"] = entries
        lines[m]["det_index"] = det_i
        n_chars += len(entries)

    return {**base, "top2_entries": n_chars, "dropped": dropped,
            "mismatch": n_mismatch, "alignment": "det_index",
            "no_index": sum(1 for d in dropped if d["reason"] == "unresolved")}


# ---------------------------------------------------------------------------
# 单页 OCR
# ---------------------------------------------------------------------------
def ocr_one(engine, png: Path) -> Tuple[List[Dict[str, Any]], List[list], Dict[str, Any], float]:
    """跑一页，返回 `(lines, det_boxes, stats, elapsed_sec)`。

    `det_boxes` 是**原始** det 框（过滤前）。若钩子失效则回落为过滤后的框，
    由调用方通过 `provenance.det_boxes_source` 标记出来。
    """
    _ALTS["cap"].clear()
    _ALTS["cap_errors"] = 0
    _REC["batches"] = []
    _REC["i"] = 0
    _REC["plan_error"] = None
    _RAW["boxes"] = None
    t0 = time.time()
    result = engine(str(png))
    elapsed = time.time() - t0

    # ⚠️ det 有框但 rec 全空时（text_score 全被滤掉 / 纯图形页），RapidOCR 返回
    #    det 形状的输出：有 boxes、**无** txts/scores 属性。不能假设它们同生。
    boxes = getattr(result, "boxes", None)
    txts = getattr(result, "txts", None) or []
    scores = getattr(result, "scores", None) or []

    lines: List[Dict[str, Any]] = []
    if result is not None and boxes is not None and txts:
        for idx, (box, txt, score) in enumerate(zip(boxes, txts, scores), start=1):
            bx, by, bw, bh = C.xywh(box)
            lines.append({
                "line_id": idx,
                "orig_line_id": idx,        # 出生点：与 line_id 相同（后续救援才可能分叉）
                "added": False,
                "bbox": [round(bx), round(by), round(bw), round(bh)],
                "text": txt,
                "confidence": round(float(score), 3),
            })

    # det 框与行**独立**收：行被空文本/分数两道过滤删掉时框仍在，差集即漏行候选。
    # 空白页/纯图页两边都为 None → 必须落成空列表，否则字段缺席会被审计当成
    # "生产者没接上"（字段存在但为空 = 这页确实没有框，是另一种语义）。
    raw = _RAW["boxes"]
    raw_quads: Optional[List[list]] = None
    det_boxes: List[list] = []
    if raw is not None:
        raw_quads = _xywh_quads(raw)
        det_boxes = raw_quads
    elif boxes is not None:                    # 钩子失效的降级路径（会被标记）
        det_boxes = _xywh_quads(boxes)

    stats: Dict[str, Any] = {"top2_entries": 0, "dropped": [], "mismatch": 0,
                             "unmatched": 0, "error": None, "cap_errors": 0,
                             "no_index": 0, "alignment": "off"}
    if _ALTS["on"]:
        try:
            stats = _assemble(lines, det_boxes, raw_quads, engine, len(lines))
            stats["error"] = None
        except Exception as e:
            # ⚠️ 这里**不能只打一行日志就算了** —— 埋点整体挂掉时产物会安静地写成
            #    "没有任何候选"，读起来像"这页确实没有摇摆位"。错误必须进 provenance
            #    并计入 `top2_errors`，由汇总响亮报出来。
            _ALTS["errors"] = int(_ALTS.get("errors", 0)) + 1
            _ALTS["last_error"] = f"{type(e).__name__}: {e}"
            print(f"[top2] ✗ 对齐失败（本页 top2 为空，**不代表没有候选**）: "
                  f"{_ALTS['last_error']}")
            stats = {"top2_entries": 0, "dropped": [], "mismatch": 0, "unmatched": 0,
                     "error": _ALTS["last_error"],
                     "cap_errors": int(_ALTS.get("cap_errors", 0)),
                     "no_index": 0, "alignment": "failed"}
    return lines, det_boxes, stats, elapsed


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m pipeline.ocr",
        description="OCR 主链路：页图 → 逐页文本行产物（含原始 det_boxes，可断点续跑）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="退出码：0 成功 / 1 有页失败或埋点失效 / 2 参数或输入错误 / 3 时间预算用尽（可续跑）",
    )
    ap.add_argument("--pages", required=True, help="页图目录（pipeline.split_pages 的产物）")
    ap.add_argument("--out", default=None, help="产物目录（默认 <pages>/../textlines）")
    ap.add_argument("--pattern", default="page_*.png", help="页图文件名模式（默认 page_*.png）")
    ap.add_argument("--pages-filter", nargs="+", default=None,
                    help="只跑这些页（页名或页号，如 page_099 或 99）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 页（抽样试跑，0 = 不限制）")
    ap.add_argument("--backend", default="auto",
                    help="auto（默认，沿用配置文件）/ openvino / onnxruntime")
    ap.add_argument("--config", default=None, help="rapidocr 完整配置文件（默认用 configs/ 下那份）")
    ap.add_argument("--max-minutes", type=float, default=0.0,
                    help="时间预算（分钟，0=不限）；到点干净退出并给出续跑命令")
    ap.add_argument("--alts", action=argparse.BooleanOptionalAction, default=True,
                    help="采集 top-2 概率（每字一条）+ 被丢弃行的现场证据。默认开；"
                         "埋点不可用时默认**拒绝运行**，要降级必须显式 --no-alts")
    ap.add_argument("--allow-filtered-det", action="store_true",
                    help="钩子失效时允许降级为过滤后的 det 框（会让审计失明，仅救急用）")
    ap.add_argument("--force", action="store_true", help="覆盖已有产物（默认断点续跑）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不构造引擎、不跑 OCR")
    return ap


def normalize_page(name: str) -> str:
    """`99` / `page_99` / `page_099` 都归一到 `page_099`。"""
    if name.isdigit():
        return f"page_{int(name):03d}"
    if name.startswith("page_") and name[5:].isdigit():
        return f"page_{int(name[5:]):03d}"
    return name


def check_reusable(dst: Path, page: str, png: Path,
                   want_fp: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """已有产物能否**直接复用**？返回 `(可复用, 不可复用原因)`。

    **判据只在这里写一份**，预检与主循环共用（两处各写一份是本项目付过代价的失败模式）。

    五种"不可复用"必须分开报，否则会静默交付过期数据：
      ① 无产物                          → 正常新跑
      ② 内容损坏（半截 JSON）           → 重跑
      ③ **页图变了**（重渲染 / 换 DPI / 换 PDF）→ 产物已过期，必须重跑
      ④ 产物 `page` 字段与文件名不符    → 目录被串用，必须重跑
      ⑤ **口径变了**（`want_fp` 与产物里记录的不一致）→ 重跑
         覆盖：产物布局版本、hook 版本、埋点开关、采集下限、后端（含 CLI 覆盖）/
         模型档位与权重哈希 / `max_side_len` / `box_thresh` / `det_boxes_source`…
    """
    if not dst.exists():
        return False, "无产物"
    d = C.load_json(dst)
    if not (isinstance(d, dict) and "lines" in d):
        return False, "已有文件损坏（半截 JSON）"
    if d.get("page") not in (None, page):
        return False, f"产物 page 字段={d.get('page')!r} 与文件名不符（目录被串用了？）"
    drift = C.image_drift(d.get("provenance"), png)
    if drift:
        return False, f"产物已过期 —— {drift}"
    if want_fp:
        diffs = C.fingerprint_drift(d.get("provenance"), want_fp)
        if diffs:
            head = "；".join(diffs[:3]) + ("…" if len(diffs) > 3 else "")
            return False, f"产物口径与当前不一致（{len(diffs)} 项）—— {head}"
    return True, ""


def book_totals(out_dir: Path, pages_dir: Path, all_pages: List[str]) -> Dict[str, Any]:
    """扫**整个产物目录**统计全书口径（而不是本次运行）。

    ⚠️ 只写"本次运行"的计数会误导：分批续跑时清单会显示 done=2，
    读起来像全书只有 2 页，而 `pages` 明细也只有本次那几页。
    ⚠️ 已知代价：每批结束都重扫全书 JSON。500 页无感，上万页要改成增量索引。
    """
    present = lines = det = low = chars = dropped = not_auditable = 0
    stale: List[str] = []
    missing: List[str] = []
    for n in all_pages:
        d = C.load_json(out_dir / f"{n}.textlines.json")
        if not (isinstance(d, dict) and "lines" in d):
            missing.append(n)
            continue
        present += 1
        prov = d.get("provenance") or {}
        if prov.get("audit_capable") is False:
            not_auditable += 1
        lines += int(d.get("line_count") or 0)
        det += len(d.get("det_boxes") or [])
        dropped += len(d.get("dropped_lines") or [])
        for ln in d.get("lines") or []:
            conf = ln.get("confidence")
            if conf is not None and conf < LOW_CONF:
                low += 1
            chars += len(ln.get("top2") or [])
        if C.image_drift(prov, pages_dir / f"{n}.png"):
            stale.append(n)
    return {
        "pages_in_dir": len(all_pages), "pages_present": present,
        "pages_missing": len(missing), "missing_pages": missing,
        "lines": lines, "det_boxes": det, "det_line_diff": max(0, det - lines),
        "low_conf": low, "top2_entries": chars, "dropped_lines": dropped,
        "not_audit_capable": not_auditable,
        "stale": len(stale), "stale_pages": stale,
    }


def peek_config(path: Optional[Path]) -> Dict[str, Any]:
    """给 dry-run 用：只解析配置文本，不构造引擎。"""
    if not path or not Path(path).exists():
        return {}
    try:
        import yaml
        d = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}
    g = d.get("Global") or {}
    det, rec = d.get("Det") or {}, d.get("Rec") or {}
    return {
        "text_score": g.get("text_score"),
        "use_cls": g.get("use_cls"),
        "max_side_len": g.get("max_side_len"),
        "use_vertical_padding": g.get("use_vertical_padding"),
        "det": f"{det.get('ocr_version')}/{det.get('model_type')} @{det.get('engine_type')}",
        "rec": f"{rec.get('ocr_version')}/{rec.get('model_type')} @{rec.get('engine_type')}",
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    C.force_utf8_stdout()

    # ---------------- 参数校验（动手之前，且把 traceback 变成人话）----------------
    if args.limit < 0:
        print(f"✗ --limit 必须 >= 0（0 = 不限制），收到 {args.limit}")
        return 2
    if args.max_minutes < 0:
        print(f"✗ --max-minutes 必须 >= 0（0 = 不限），收到 {args.max_minutes}")
        return 2
    if (args.backend or "auto").strip().lower() not in ("auto", "", "default",
                                                       "openvino", "onnxruntime"):
        print(f"✗ --backend 取值非法：{args.backend!r}（可选 auto / openvino / onnxruntime）")
        return 2

    # ---------------- 兼容层版本闸门（hook 的是 rapidocr 内部结构）----------------
    try:
        rc_ver = md.version("rapidocr")
    except Exception:
        rc_ver = "?"
    if rc_ver not in SUPPORTED_RAPIDOCR:
        print(f"✗ rapidocr {rc_ver} 未经验证（实测可用：{', '.join(SUPPORTED_RAPIDOCR)}）")
        print("  本模块 hook 的是 rapidocr 的**内部**实现：map_boxes_to_original /")
        print("  TextRecognizer 的 argsort 分批 / CTCLabelDecode。这些一变，")
        print("  后果不是「结果略变」而是**审计证据直接消失**，所以拒绝在未验证版本上生产。")
        print(f"  确认要在该版本上跑：先把 {rc_ver} 加进 SUPPORTED_RAPIDOCR 并重跑回归。")
        return 2

    pages_dir = Path(args.pages)
    if not pages_dir.is_dir():
        print(f"✗ 页图目录不存在：{pages_dir}")
        return 2
    out_dir = Path(args.out) if args.out else pages_dir.parent / "textlines"
    if out_dir.exists() and not out_dir.is_dir():
        print(f"✗ --out 已存在但不是目录：{out_dir}")
        return 2

    all_pages = C.list_pages(pages_dir, args.pattern)
    if not all_pages:
        print(f"✗ {pages_dir} 里没有匹配 {args.pattern} 的页图")
        return 2
    names = all_pages
    if args.pages_filter:
        want = {normalize_page(x) for x in args.pages_filter}
        miss = sorted(want - set(all_pages))
        names = [n for n in all_pages if n in want]
        if miss:
            print(f"⚠ 有 {len(miss)} 页不在页图目录里，已忽略：{', '.join(miss[:6])}"
                  f"{' …' if len(miss) > 6 else ''}")
        if not names:
            print("✗ --pages-filter 过滤后没有可跑的页")
            return 2
    if args.limit:
        names = names[: args.limit]

    cfg_path = Path(args.config) if args.config else C.default_config_path()

    # ---------------- 埋点能力探测（先探，再算指纹）----------------
    # 🔴 顺序很关键：`alts_enabled` / `det_boxes_source` **必须**在算指纹之前定下来。
    #    早先的写法是"先算指纹、再装钩子，装不上就把 args.alts 改掉" ——
    #    结果产物实际没采 top2，provenance 却声称是 top2 口径，复用判据也跟着错。
    alts_on = bool(args.alts)
    if alts_on:
        if not install_rec_index_hook():
            print("\n✗ 无法挂钩 TextRecognizer.__call__ —— 拿不到 rec 批次与 det 框的下标映射。")
            print("  没有它，top2 只能靠**文本**反查回行：同一页出现两次相同文本时，")
            print("  谁挂到哪一行是任意的 —— 那等于把 A 行的模型证据挂到 B 行的框上。")
            print("  这类证据不采比采错好，所以拒绝运行。确认不要 top2 请显式加 --no-alts。")
            return 1
        if not install_alts_hook():
            print("\n✗ 无法挂钩 CTCLabelDecode.__call__ —— top2 埋点不可用。")
            print("  这是形近字分析唯一的证据源（那类错字 conf 高达 0.98+，阈值判不出来）。")
            print("  确认不要 top2 请显式加 --no-alts。")
            return 1
    det_source = "raw" if install_raw_det_hook() else "filtered"

    # 口径指纹：**不构造引擎**就能算（预检阶段还没有引擎）。
    want_fp = C.ocr_fingerprint(cfg_path, alts=alts_on, top2_floor=TOP2_FLOOR,
                                top2_decimals=TOP2_DECIMALS, backend=args.backend,
                                det_boxes_source=det_source, hook_schema=HOOK_SCHEMA)
    if want_fp.get("_cfg_error"):
        # 🔴 指纹不完整 ⇒ 复用判据会在"没比到"的情况下给出"一致"的结论。
        #    这对 OCR 生产产物不可接受，必须拒绝而不是降级运行。
        print(f"✗ 配置文件无法解析：{want_fp['_cfg_error']}")
        print(f"  配置：{cfg_path}")
        print("  口径指纹不完整时，断点续跑会把**不同口径**的旧产物当成本次的产物复用。")
        return 2

    # ---------------- 预检：哪些页可直接复用（判据与主循环同一份）----------------
    reusable: Dict[str, bool] = {}
    reason: Dict[str, str] = {}
    if not args.force:
        for n in names:
            ok, why = check_reusable(out_dir / f"{n}.textlines.json", n,
                                     pages_dir / f"{n}.png", want_fp)
            reusable[n], reason[n] = ok, why
    todo = [n for n in names if not (not args.force and reusable.get(n))]
    # 有产物但不可复用（页图变了 / 损坏 / 页名不符）—— 与"新跑"分开列出来，
    # 因为这两种情况的心理预期完全不同：前者容易被误当成"已经跑过了"。
    stale_n: List[str] = []
    if not args.force:
        stale_n = [n for n in names
                   if not reusable.get(n, False) and (out_dir / f"{n}.textlines.json").exists()]

    print(f"页图目录 : {pages_dir}   （{len(names)} 页待处理）")
    print(f"产物目录 : {out_dir}")
    print(f"配置文件 : {cfg_path}")
    print(f"rapidocr : {rc_ver}（已验证）   hook_schema={HOOK_SCHEMA}")
    if stale_n:
        print(f"⚠ {len(stale_n)} 页已有产物但**不可复用**（页图变了 / 损坏 / 口径变了）"
              f"，将重跑：{', '.join(stale_n[:6])}{' …' if len(stale_n) > 6 else ''}")
    if args.dry_run:
        cfg = peek_config(cfg_path)
        if cfg.get("_error"):
            print(f"  配置解析失败：{cfg['_error']}")
        elif cfg:
            print(f"  det={cfg['det']}   rec={cfg['rec']}")
            print(f"  text_score={cfg['text_score']}  use_cls={cfg['use_cls']}"
                  f"  max_side_len={cfg['max_side_len']}"
                  f"  use_vertical_padding={cfg['use_vertical_padding']}")
        print(f"后端     : {args.backend}（auto = 沿用配置文件的 engine_type；"
              f"dry-run 不构造引擎）")
        print(f"埋点     : top2 {'开' if alts_on else '关'}"
              f" + 丢弃行证据 {'开' if alts_on else '关'}"
              f"   det 框来源：{'原始（过滤前）' if det_source == 'raw' else '过滤后（降级）'}")
        print(f"\n[dry-run] 将处理 {len(names)} 页，其中 {len(todo)} 页需新跑"
              f"、{len(names) - len(todo)} 页可复用：")
        for n in names[:12]:
            flag = "" if n in todo else "（可复用，跳过）"
            if n in todo and (out_dir / f"{n}.textlines.json").exists():
                flag = f"（{reason.get(n) or '不可复用'}，重跑）"
            print(f"  {n}{flag}")
        if len(names) > 12:
            print(f"  … 其余 {len(names) - 12} 页")
        return 0
    if not todo:
        print(f"\n全部 {len(names)} 页都可复用，无需处理（--force 可强制重跑）")
        return 0

    # ---------------- 真要跑了：降级检查 + 锁 ----------------
    if det_source != "raw" and not args.allow_filtered_det:
        print("\n✗ 无法挂钩 `rapidocr.main.map_boxes_to_original` —— 拿不到**过滤前**的 det 框。")
        print("  用 `result.boxes` 落盘会让漏行审计结构性失明（框与行永远 1:1）。")
        print("  所以默认拒绝产出。请检查 rapidocr 版本是否改了内部结构；")
        print("  确认要降级就加 --allow-filtered-det（产物会被标记 audit_capable=False）。")
        return 1
    if det_source != "raw":
        print("\n⚠ 已按 --allow-filtered-det 降级：det_boxes 是**过滤后**的框，"
              "漏行审计将无法发现漏行；产物会标记 audit_capable=False。")

    _ALTS["on"] = alts_on
    _ALTS["errors"] = 0            # 每次运行重算，别让上一次的残留冒充本次
    _ALTS["last_error"] = None

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"✗ 无法创建产物目录 {out_dir}：{type(e).__name__}: {e}")
        return 2
    lock = C.OutputLock(out_dir / LOCKFILE)
    if not lock.acquire():
        print(f"✗ 已有另一个 pipeline.ocr 正在写 {out_dir}（{LOCKFILE} 被占用）")
        print("  并发运行会让 ocr_manifest.json 互相覆盖（磁盘 100 页、清单只剩 50 页），")
        print("  请等它跑完再重试。")
        return 2
    try:
        return _run(args, pages_dir, out_dir, cfg_path, names, all_pages, todo,
                    alts_on, det_source, want_fp, rc_ver)
    finally:
        lock.release()


def _run(args, pages_dir: Path, out_dir: Path, cfg_path: Path, names: List[str],
         all_pages: List[str], todo: List[str], alts_on: bool, det_source: str,
         want_fp: Dict[str, Any], rc_ver: str) -> int:
    # ---------------- 构造引擎 ----------------
    print("\n构造引擎（首次会下载模型 / OpenVINO 首次编译较慢）...")
    t_init = time.time()
    try:
        engine = C.build_engine(backend=args.backend, config_path=cfg_path)
    except Exception as e:
        print(f"✗ 引擎构造失败：{type(e).__name__}: {e}")
        return 1
    init_sec = time.time() - t_init
    # 溯源走**同一份**指纹（与 `want_fp` 逐键可比）—— 否则"写入的键"和"比对的键"
    # 是两套，复用判据会永远判"不一致"或永远判"一致"。
    prov = dict(want_fp)
    d_ver, d_type = prov["det"].split("/")
    print(f"  就绪 {init_sec:.1f}s   后端={prov['backend']}  det={prov['det']}  rec={prov['rec']}")
    engine_name = f"rapidocr-{rc_ver}/{prov['backend']}/{d_ver}-{d_type}"

    # ---------------- 逐页 ----------------
    budget = args.max_minutes * 60
    t_all = time.time()
    records: List[Dict[str, Any]] = []
    n_done = n_skip = n_fail = n_lines = n_low = n_diff = 0
    n_chars_all = n_drop = n_mismatch = n_dropdrift = n_unres = 0
    n_t2err = n_t2zero = n_caperr = 0
    n_norec: List[str] = []          # det 有框但 rec 全空 —— 高度可疑（真漏行信号）
    n_fig_empty: List[str] = []      # 同上，但判为图版页 → 艺术字 rec 空属预期，不算信号
    n_empty: List[str] = []          # det 也无框（空白页/纯图页）
    n_drop_text: List[str] = []      # 丢弃行里有非空文本 —— 真内容被 text_score 滤掉
    fig_cache: Dict[str, Optional[bool]] = {}
    n_fig_unknown: List[str] = []    # 图版页判据**判不了**（解码失败等）—— 不是"不是图版页"
    interrupted = False

    for k, name in enumerate(names, 1):
        dst = out_dir / f"{name}.textlines.json"
        png = pages_dir / f"{name}.png"

        if not args.force and check_reusable(dst, name, png, want_fp)[0]:
            prev = C.load_json(dst) or {}
            n_skip += 1
            n_lines += int(prev.get("line_count") or 0)
            print(f"[{k}/{len(names)}] {name}: 可复用，跳过")
            continue

        if budget and (time.time() - t_all) > budget:
            interrupted = True
            print(f"\n⏸ 时间预算 {args.max_minutes:g} 分钟用尽，在 {name} 前停下。")
            break

        try:
            lines, det_boxes, stats, elapsed = ocr_one(engine, png)
        except Exception as e:
            n_fail += 1
            print(f"[{k}/{len(names)}] {name}: ✗ OCR 失败 {type(e).__name__}: {e}")
            continue

        dumped = stats["dropped"]
        n_chars = stats["top2_entries"]
        n_mis = stats["mismatch"]
        n_un = stats["no_index"]
        if stats["error"]:
            n_t2err += 1
        if stats.get("cap_errors"):
            n_caperr += stats["cap_errors"]
        if alts_on and stats["error"] is None and n_chars == 0 and lines:
            # 埋点没报错、但一个候选都没有 —— 可能真是一整页都很确定，也可能是
            # 门槛/口径写错。单独记账，别让它混进"这页很正常"里。
            n_t2zero += 1

        # 交叉校验：`dropped` 现在是**直接推导**出来的，这里用 det−行 再对一次账。
        # 不等 ⇒ 下标链路某一环与预期不符，必须报出来 —— 否则 dropped_lines
        # 会安静地少记，别人以为"漏行没有证据"。
        expect_drop = max(0, len(det_boxes) - len(lines))
        drop_drift = bool(alts_on) and det_source == "raw" and len(dumped) != expect_drop
        if drop_drift:
            n_dropdrift += 1
            print(f"[{k}/{len(names)}] {name}: ⚠ 丢弃行自校验失败 —— "
                  f"det {len(det_boxes)} − 行 {len(lines)} = {expect_drop}，"
                  f"但推导出 {len(dumped)} 条")
        if n_mis:
            n_mismatch += n_mis
            print(f"[{k}/{len(names)}] {name}: ⚠ {n_mis} 行的文本重建与 rec 实际输出不一致，"
                  f"这些框标为 unresolved（下标不可信）")
        if n_un:
            n_unres += n_un
            print(f"[{k}/{len(names)}] {name}: ⚠ {n_un} 个 det 框没有可信的 rec 行"
                  f"（捕获失败 / 文本重建不一致），已标为 unresolved")

        doc = {
            "page": name,
            "engine": engine_name,
            "line_count": len(lines),
            "elapsed_sec": round(elapsed, 1),
            "provenance": {**prov, **C.image_fingerprint(png),
                           # 🔴 审计能力标记：det_boxes 是过滤后的框时，漏行审计
                           #    结构性地看不到"有框无行"，下游必须据此拒绝消费。
                           "audit_capable": det_source == "raw",
                           "alignment": stats["alignment"],
                           "alt_capture": bool(alts_on),
                           "top2_floor": TOP2_FLOOR if alts_on else None,
                           "top2_entries": n_chars,
                           "top2_error": stats["error"],
                           "top2_capture_errors": stats.get("cap_errors", 0),
                           "det_index_unresolved": n_un,
                           "text_mismatch_lines": n_mis,
                           "dropped_captured": len(dumped),
                           "dropped_expected": expect_drop,
                           "drop_selfcheck": "ok" if not drop_drift else "drift"},
            "lines": lines,
            "det_boxes": det_boxes,        # 字段必须存在（可为空列表）
            # `null` = 本次没采集（`--no-alts`）；`[]` = 采集了、确实没有丢弃行。
            # 两者必须能区分 —— 与 `det_boxes` 同一套语义。
            "dropped_lines": dumped if alts_on else None,
        }
        C.write_product_json(dst, doc)     # 逐点数组压成单行（见 common.dumps_product_json）

        low = sum(1 for l in lines if l["confidence"] < LOW_CONF)
        diff = max(0, len(det_boxes) - len(lines))
        n_done += 1
        n_lines += len(lines)
        n_low += low
        n_chars_all += n_chars
        n_diff += diff
        n_drop += len(dumped)
        if not lines and det_boxes:
            # 先判图版页再报"可疑"：图版页的艺术字 rec 必然空，那是预期不是漏行。
            # 只在这种情况下才解码 PNG，所以正常页不付这个成本。
            # ⚠️ `is_figure_page` 返回 Optional[bool]：`None` = **判不了**（解码失败等），
            #    必须单独成类 —— 当成 False 会把一张读不出来的图版页误报成"漏行可疑"，
            #    当成 True 又会把真漏行藏起来。两者都是在编造结论。
            fig = C.is_figure_page(png, cache=fig_cache)
            if fig is True:
                n_fig_empty.append(name)
            elif fig is None:
                n_fig_unknown.append(name)
            else:
                n_norec.append(name)
        elif not lines and not det_boxes:
            n_empty.append(name)
        # 被丢弃但有**非空文本**的：rec 读出了内容，是 `text_score` 过滤掉的
        # 真内容 —— 与「rec 完全失败（空文本）」是两回事，分开记账。
        if any((d.get("text") or "").strip() for d in dumped):
            n_drop_text.append(name)
        records.append({"name": name, "lines": len(lines), "det_boxes": len(det_boxes),
                        "diff": diff, "low_conf": low, "top2_entries": n_chars,
                        "dropped": len(dumped), "alignment": stats["alignment"],
                        "sec": round(elapsed, 2)})
        print(f"[{k}/{len(names)}] {name}: {len(lines)} 行 / det {len(det_boxes)} 框"
              f" / 差 {diff} / 丢弃 {len(dumped)} / 低 conf {low}"
              f" / top2 {n_chars} 字  {elapsed:.1f}s")

    # ---------------- 汇总 + 留痕 ----------------
    elapsed_all = time.time() - t_all
    print(f"\n{'⏸ 未完成' if interrupted else 'DONE'} 本次：跑 {n_done} 页｜复用 {n_skip}"
          f"｜失败 {n_fail}｜新增 {n_lines} 行｜低 conf(<{LOW_CONF}) {n_low}"
          f"｜top2 {n_chars_all} 条（下限 {TOP2_FLOOR:g}）｜丢弃证据 {n_drop} 条"
          f"｜耗时 {elapsed_all:.1f}s")
    if n_done:
        print(f"  页均 {elapsed_all / n_done:.2f}s（不含引擎初始化 {init_sec:.1f}s）")

    if not alts_on:
        print("\n⚠ --no-alts：本次**没有**采集 top2 与丢弃行证据，"
              "形近字分析与漏行定位都缺少证据源。")
    if n_diff:
        print(f"\n⚠ 本次产物里「det 有框 / 无行」共 {n_diff} 处 —— 这就是漏行候选，"
              f"证据在产物的 `dropped_lines` 里（含精确 bbox）：")
        print(f"  {', '.join(r['name'] for r in records if r['diff'])[:200]}")
    if n_drop_text:
        print(f"\n⚠ {len(n_drop_text)} 页的丢弃行带**非空文本** —— rec 读出了内容却被 "
              f"`text_score` 滤掉，是真内容丢失（不是 rec 失败）：\n"
              f"  {', '.join(n_drop_text[:10])}{' …' if len(n_drop_text) > 10 else ''}")
    if n_dropdrift:
        print(f"\n⚠ {n_dropdrift} 页丢弃行自校验失败（推导出的条数与 det−行 不等）"
              f"—— 下标链路与过滤口径对不上，别信这些页的 dropped_lines。")
    if n_unres:
        print(f"\n⚠ {n_unres} 个 det 框没有可信的 rec 行（标为 unresolved）—— "
              f"这些框的 top2 是**没有采到**，不是「没有候选」。")
    if n_t2err:
        print(f"\n🔴 {n_t2err} 页的 top2 埋点**抛异常**（这些页的 top2 是空的，"
              f"不代表没有候选）：{_ALTS.get('last_error')}"
              f"\n   埋点出问题时产物会安静地写空 —— 必须修，别拿这些页做形近字分析。")
    elif n_t2zero:
        print(f"\n⚠ {n_t2zero} 页有正文但 top2 **一条候选都没有**（下限 {TOP2_FLOOR:g}）"
              f"—— 可能整页确实很确定，也可能是门槛/口径写错，抽查一页确认。")
    if n_caperr:
        print(f"\n⚠ 埋点内部有 {n_caperr} 次捕获失败（部分批次）—— 这些页**不报错、也不整页变空**，"
              f"但某些行会缺候选；半残状态与「这些行确实没有候选」看起来一样，必须查。")
    if n_mismatch:
        print(f"\n⚠ {n_mismatch} 行文本重建与 rec 实际输出不一致 → 未挂 top2。"
              f"多半是 CTC 解码口径变了（含空格/字典表），需要查。")
    if n_norec:
        print(f"\n⚠ {len(n_norec)} 页「det 有框但 rec 全空」—— `text_score` 静默丢弃的"
              f"重灾区：{', '.join(n_norec[:10])}{' …' if len(n_norec) > 10 else ''}")
    if n_fig_empty:
        print(f"\nℹ {len(n_fig_empty)} 页「有框无行」但判为图版页（艺术字 rec 空属预期，"
              f"不是漏行信号）：{', '.join(n_fig_empty[:8])}")
    if n_fig_unknown:
        print(f"\n⚠ {len(n_fig_unknown)} 页「有框无行」且**图版页判据判不了**"
              f"（页图无法解码/读取）—— 既不能算漏行也不能算预期，请先确认页图本身："
              f"{', '.join(n_fig_unknown[:8])}")
    if n_empty:
        print(f"\nℹ {len(n_empty)} 页 det 无框（空白页/纯图页，正常）：{', '.join(n_empty[:8])}")

    remaining = len(names) - n_done - n_skip
    if remaining > 0 and not n_fail:
        print(f"\n剩余 {remaining} 页未跑，续跑同一条命令即可（可复用的页会自动跳过）。")
    if n_fail:
        print(f"\n⚠ 有 {n_fail} 页 OCR 失败 —— 这些页在产物里会缺席，别急着往下走。")

    # 清单按**全书**口径写（不是本次运行），并保留本次未处理页的历史明细 ——
    # 否则分批续跑后读清单会以为全书只有几页。
    book = book_totals(out_dir, pages_dir, all_pages)
    prev_manifest = C.load_json(out_dir / MANIFEST) or {}
    merged: Dict[str, Dict[str, Any]] = {
        p.get("name"): p for p in (prev_manifest.get("pages") or [])
        if isinstance(p, dict) and p.get("name")
    }
    for r in records:
        merged[r["name"]] = r
    C.dump_json(out_dir / MANIFEST, {
        "pages_dir": str(pages_dir), "pattern": args.pattern, "out_dir": str(out_dir),
        "engine": engine_name, "provenance": prov, "rapidocr": rc_ver,
        "hook_schema": HOOK_SCHEMA, "audit_capable": det_source == "raw",
        "alt_capture": bool(alts_on), "init_sec": round(init_sec, 2),
        "this_run": {"done": n_done, "reused": n_skip, "failed": n_fail,
                     "lines": n_lines, "low_conf": n_low,
                     "top2_entries": n_chars_all, "dropped_lines": n_drop,
                     "top2_floor": TOP2_FLOOR,
                     "top2_errors": n_t2err, "top2_zero_pages": n_t2zero,
                     "top2_capture_errors": n_caperr,
                     "last_top2_error": _ALTS.get("last_error"),
                     "drop_selfcheck_failed": n_dropdrift,
                     "det_index_unresolved": n_unres,
                     "text_mismatch_lines": n_mismatch,
                     "det_line_diff": n_diff, "elapsed_sec": round(elapsed_all, 1)},
        "book": book,
        "suspect_no_rec": n_norec, "figure_pages_empty": n_fig_empty,
        "figure_judgement_unknown": n_fig_unknown,
        "empty_pages": n_empty, "dropped_with_text": n_drop_text,
        "pages": [merged[k] for k in sorted(merged)],
    })
    print(f"  留痕   -> {out_dir / MANIFEST}")
    print(f"  全书   -> {book['pages_present']}/{book['pages_in_dir']} 页有产物"
          f"｜{book['lines']} 行｜det {book['det_boxes']} 框｜差 {book['det_line_diff']}"
          f"｜低 conf {book['low_conf']}｜top2 {book['top2_entries']} 字"
          f"｜丢弃证据 {book['dropped_lines']} 条" +
          (f"｜**过期 {book['stale']} 页**" if book["stale"] else "") +
          (f"｜**不可审计 {book['not_audit_capable']} 页**" if book["not_audit_capable"] else ""))

    if n_fail:
        return 1
    return 3 if interrupted else 0


if __name__ == "__main__":
    raise SystemExit(main())
