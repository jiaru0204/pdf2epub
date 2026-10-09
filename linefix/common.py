# -*- coding: utf-8 -*-
"""产线各阶段共用的唯一实现层。

**口径只写一份**：判据、几何、原子写、产物 JSON 序列化、页图身份/过期判定、
引擎构造与缓存指纹、输出目录锁 —— 这些一旦各阶段各写一套，就会出现
"同一份数据两处口径不同"的经典失败（本项目历史上 02c/02d 各复制一份，
与 `load_tl_aligned` 两份实现是同一个坑）。

目前覆盖的阶段：`pipeline.split_pages`（拆页）、`pipeline.ocr`（OCR 主链路）。

⚠️ 本模块**只放已经在用的东西**。后续阶段（漏行审计 / 局部救援 / 字级复核）
需要的接口等做到那一步再加，不留没人调用的空壳。
"""
from __future__ import annotations

import hashlib
import importlib.metadata as md
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

# ---------------------------------------------------------------------------
# ① 判据：图片页
# ---------------------------------------------------------------------------
#: 图片页判据：暗像素占比超过此值视为图版页（艺术字 rec 必然失败，非漏行）。
FIG_INK = 0.45
FIG_INK_GRAY_CUT = 160


def is_figure_page(
    png_path: Union[str, Path],
    ink_thresh: float = FIG_INK,
    cache: Optional[Dict[str, bool]] = None,
) -> bool:
    """图片页判据：暗像素占比 > 阈值。

    `cache` 传入一个 dict 即可复用结果（同一页在多处扫描时只算一次）。
    cache 的 key 会做**绝对路径归一化** —— 否则 `./pages/a.png` 与
    `pages/a.png` 会被当成两个条目，缓存形同虚设。
    """
    key = os.path.normcase(os.path.abspath(str(png_path)))
    if cache is not None and key in cache:
        return cache[key]
    from PIL import Image
    try:
        g = np.asarray(Image.open(png_path).convert("L"))
        val = bool((g < FIG_INK_GRAY_CUT).mean() > ink_thresh)
    except Exception:
        val = False
    if cache is not None:
        cache[key] = val
    return val


# ---------------------------------------------------------------------------
# ② 几何
# ---------------------------------------------------------------------------
def xywh(box) -> Tuple[float, float, float, float]:
    """四点框 / 任意 (N,2) 形状 → (x, y, w, h)。"""
    b = np.asarray(box, dtype=float).reshape(-1, 2)
    x0, y0 = float(b[:, 0].min()), float(b[:, 1].min())
    x1, y1 = float(b[:, 0].max()), float(b[:, 1].max())
    return x0, y0, x1 - x0, y1 - y0


# ---------------------------------------------------------------------------
# ③ 落盘：**原子写**是全局约定（半截文件绝不能留给下游）
# ---------------------------------------------------------------------------
def atomic_write_bytes(path: Union[str, Path], data: bytes) -> None:
    """原子写二进制：先写 `<名>.tmp` 再 `os.replace`。

    ⚠️ 为什么不能省：半截 JSON 的危害不是"多一个坏文件"，而是**下游可能把它当合法产物**。
    本项目所有写盘（页图、产物、清单）都走这里，口径只写一份。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, p)


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
# ④ 产物 JSON 读写（格式唯一来源）
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
    for k, v in holders.items():
        s = s.replace(k, v)
    return s


def write_product_json(path: Union[str, Path], doc: Dict[str, Any]) -> None:
    """原子写文本行产物（紧凑逐点数组）。**产物一律走这里**，别直接 json.dumps。"""
    atomic_write_text(path, dumps_product_json(doc))


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
# ⑤ 页图身份与"产物是否过期"
# ---------------------------------------------------------------------------
def image_fingerprint(png: Union[str, Path]) -> Dict[str, Any]:
    """页图身份：字节数 + mtime_ns + 内容哈希。写进产物 `provenance`。"""
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
    """
    if not isinstance(prov, dict):
        return "产物没有 provenance"
    if not prov.get("png_sha256"):
        return "产物没记页图哈希（旧版本产物）"
    try:
        st = Path(png).stat()
    except OSError:
        return None                      # 页图不在 → 由调用方另行报"缺页面图"
    if int(prov.get("png_bytes") or -1) != int(st.st_size):
        return f"页图字节数变了（{prov.get('png_bytes')} → {st.st_size}）"
    if prov["png_sha256"] != file_sha256(png):
        return "页图内容变了（SHA256 不符）"
    return None


# ---------------------------------------------------------------------------
# ⑥ 引擎构造与缓存指纹
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

    ⚠️ `use_cls=False` **不会**阻止 TextClassifier 被构造，所以 Cls 段的
       `engine_type` 也必须指向已安装的后端（否则 ImportError）。
    ⚠️ 用 `params=` 时枚举值必须传 `EngineType` 枚举，传字符串会 TypeError。
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
    """把当前引擎的配置抓成指纹，**用于写进产物做溯源**（需要已有引擎对象）。"""
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
      · 后端：`backend`（**含 CLI 覆盖**，见下面 ⚠️）+ `backend_override`
      · 模型：det / rec / cls 档位 + **权重文件实际 SHA256**
      · 配置：`max_side_len` / `box_thresh` / `text_score` / `use_cls` …
      · 本模块口径：`product_schema` / `hook_schema` / `alt_capture` /
        `top2_floor` / `top2_decimals` / `det_boxes_source`

    ⚠️ 历史坑一：判据原来只比页图 —— 于是**改完采集口径，旧产物照样被判"可复用"**
    （实测：旧口径产物缺 `top2`/`dropped_lines`/`top2_floor`，仍报"全部可复用"）。

    ⚠️ 历史坑二：`--backend` 原来**没有进指纹** —— 配置里写 openvino，同一条命令加
    `--backend onnxruntime` 仍被判"可复用"，产物无法回答"这页到底是哪个后端跑的"。
    现在 `backend_override` 显式记录 CLI 取值，`backend` 记录**生效值**。

    ⚠️ 必须能在**没有引擎**时算出来：预检阶段若比不了，就会出现"预检说全部可复用 →
    直接 return 0"，根本没机会比引擎参数。

    ⚠️ `det_boxes_source` 也进指纹：`raw` 与 `filtered` 的产物**审计能力不同**，
    不能互相复用（filtered 的产物 `linefix.audit` 结构性失明）。
    """
    c = cfg_path or default_config_path()
    override = (backend or "auto").strip().lower()
    out: Dict[str, Any] = {"product_schema": PRODUCT_SCHEMA,
                           "hook_schema": int(hook_schema),
                           "alt_capture": bool(alts),
                           "top2_floor": top2_floor if alts else None,
                           "top2_decimals": top2_decimals if alts else None,
                           "det_boxes_source": det_boxes_source,
                           "backend_override": override}
    out.update(_versions())
    if not c:
        out["_cfg_error"] = f"找不到配置文件（{DEFAULT_CONFIG_REL}）"
        return out
    try:
        from rapidocr.utils.parse_parameters import ParseParams
        cfg_obj = ParseParams().load(str(c))
        out.update(_fingerprint_from_cfg_obj(cfg_obj))
        out["model_sha256"] = _model_files(cfg_obj)
        if override not in ("auto", "", "default"):
            # 生效后端 = CLI 覆盖值（build_engine 会在三段上强制覆盖）
            out["backend"] = override
    except Exception as e:
        # 解析不出来**绝不猜参数**：猜出来的指纹会让复用判据给出错误结论
        # （"看着一致"其实没比到）。调用方见到 `_cfg_error` 必须**拒绝运行**，
        # 而不是降级 —— 见 pipeline/ocr.py。
        out["_cfg_error"] = f"{type(e).__name__}: {e}"
    return out


def fingerprint_drift(stored, want: Dict[str, Any]) -> List[str]:
    """逐键比对指纹，返回差异说明（空列表 = 一致）。只比 `want` 里出现的键。"""
    stored = stored or {}
    out: List[str] = []
    for k, v in want.items():
        if k.startswith("_"):
            continue
        got = stored.get(k, "<缺>")
        if got != v:
            out.append(f"{k}: {got!r} → {v!r}")
    return out


# ---------------------------------------------------------------------------
# ⑦ 输出目录独占锁（manifest / 汇总 JSON 都是 read-modify-write）
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
# ⑧ 杂项
# ---------------------------------------------------------------------------
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
