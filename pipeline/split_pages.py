# -*- coding: utf-8 -*-
"""拆页：PDF → 逐页 PNG（300 DPI）。

它是整条产线的第一环，也是「漏行审计」的前置——`linefix.audit` 要读产物里的
`det_boxes` 字段，而产物由 OCR 主链路从这些页图产出。

用法
----
    python -m pipeline.split_pages --pdf <文件.pdf>              # 输出到 <pdf 同目录>/pages
    python -m pipeline.split_pages --pdf a.pdf --out <目录> --dpi 300
    python -m pipeline.split_pages --pdf a.pdf --pages 1,5,10-20  # 只渲染指定页（抽样试跑）
    python -m pipeline.split_pages --pdf a.pdf --dry-run          # 只列出将渲染的页与尺寸

设计要点（沿用本项目既有约束）
------------------------------
1. **断点续跑**：跳过一页的**充分条件**是「该页在 manifest 里有记录、记录与本次的
   来源/配置一致、且磁盘上的 PNG 头尺寸与记录自洽」三者同时成立。
   ⚠️ 「PNG 存在」单独**不构成**跳过的理由（见下条）。
2. **来源留痕 + fail-closed**：写 `<out>/split_manifest.json`，记录 PDF 路径 / **SHA256** /
   体积 / DPI / 渲染器版本 / 前缀 / 页名宽度 / 每页像素尺寸。
   影响产物内容或命名的参数（pdf_sha256 / dpi / renderer / prefix / digits）只要与既有
   manifest 不一致就**拒绝续跑**；manifest 缺失但目录里已有 PNG、或 manifest 损坏，
   同样拒绝——宁可让用户换目录，也不把来源不明的页图当成自己的产物。
   `--force` **只**表示「同来源、同配置，重渲已存在的页」，不再具有"接受另一份 PDF"的权限。
3. **原子写盘**：先写**唯一名**临时文件再 `os.replace`（实现见 `linefix.common.atomic_write_bytes`），
   避免半截文件被下游当成合法产物。
4. **并发保护**：`<out>/.split_pages.lock` 独占锁（OS advisory，进程退出即释放）。
   manifest 是 read-modify-write，两个进程同时跑会互相覆盖。
5. **页名对齐**：`page_001.png`（默认 3 位），页数超过 999 时自动加宽。

⚠️ 渲染器是 PyMuPDF（AGPL-3.0）。它只在这个文件里出现两处（`render_page()` /
`renderer_tag()`），换渲染器只需改这两处（例如换 pypdfium2，Apache-2.0/BSD）。
   注意换渲染器会改变像素，与既有产物的 bbox 不再逐位可比——manifest 会把
   `renderer` 记为身份的一部分，因此换渲染器后指向同一目录会被拒绝续跑（符合预期）。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pymupdf

# 与产线其它阶段共用的实现层（原子写 / 文件哈希 / JSON 读写只在这里一份）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from linefix import common as C  # noqa: E402

MANIFEST = "split_manifest.json"
LOCKFILE = ".split_pages.lock"
DEFAULT_DPI = 300
DEFAULT_PREFIX = "page"

# 影响「产物内容」或「产物命名」的参数。只要与既有 manifest 不一致，就说明
# 目录里躺着另一套口径的页图，必须拒绝续跑，否则 manifest 会替磁盘"撒谎"。
IDENTITY_KEYS = ("pdf_sha256", "dpi", "renderer", "prefix", "digits")

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# ① 渲染（整个文件里唯一依赖 PyMuPDF 的地方之一）
# ---------------------------------------------------------------------------
def render_page(page, dpi: int) -> bytes:
    """把一页渲染成 PNG 字节。换渲染器只动这里。"""
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    return pix.tobytes("png")


def renderer_tag() -> str:
    """渲染器指纹（进 manifest，参与续跑身份校验）。换渲染器只动这里。"""
    ver = getattr(pymupdf, "__version__", "")
    return f"pymupdf {ver}".strip()


def pixmap_size(page, dpi: int) -> Tuple[int, int]:
    """预测该页渲染后的像素尺寸（不需要真渲染）。

    ⚠️ pymupdf 用的是 **`ceil`**（IRect 取整），不是 `round`：
    750.047pt × 300/72 = 3125.196 → 实际 pixmap 宽 **3126**（round 会得 3125）。
    已在 5 本书 25 页上逐页核对，与 `get_pixmap().width/height` 零偏差。
    """
    z = dpi / 72.0
    r = page.rect
    return (math.ceil(r.x1 * z) - math.floor(r.x0 * z),
            math.ceil(r.y1 * z) - math.floor(r.y0 * z))


# ---------------------------------------------------------------------------
# ② PNG 头解析：判"这个文件到底是不是一页合法的、尺寸为 W×H 的 PNG"
# ---------------------------------------------------------------------------
def png_size(data: bytes) -> Optional[Tuple[int, int]]:
    """从 PNG 的 IHDR 读真实像素尺寸（大端 uint32）。不依赖任何图像库。

    校验到 IHDR 为止：magic + IHDR 块长度必须为 13 + 宽高为正。
    这对本文件的用途是够的（只用于校验"自己渲染出来的、或尺寸可预测的" PNG），
    不构成对任意外部 PNG 的完整校验。
    """
    if len(data) < 24 or data[:8] != PNG_MAGIC:
        return None
    if data[8:12] != (13).to_bytes(4, "big") or data[12:16] != b"IHDR":
        return None
    w = int.from_bytes(data[16:20], "big")
    h = int.from_bytes(data[20:24], "big")
    if w <= 0 or h <= 0:
        return None
    return w, h


def png_size_file(path: Path) -> Optional[Tuple[int, int]]:
    """只读前 24 字节，判断一个**既有** PNG 的真实尺寸。

    文件不存在 / 截断（2KB 的坏文件也算）/ 不是 PNG / 尺寸非法 → None。
    成本极低，不需要 Pillow，也不用解码整图。
    """
    try:
        with path.open("rb") as f:
            head = f.read(24)
    except OSError:
        return None
    return png_size(head)


# ---------------------------------------------------------------------------
# ③ 页码规格解析
# ---------------------------------------------------------------------------
def parse_page_spec(spec: Optional[str], n: int,
                    dropped: Optional[List[int]] = None) -> List[int]:
    """'1,5,10-20' → [1,5,10..20]（1-based，去重保序，越界丢弃）。空 = 全部。

    非法输入抛 ValueError，由 CLI 层转成用户可读的错误（而不是甩一个 traceback）。
    `dropped`：可选的出参，把越界页码收集进去，供上层提示"忽略了哪些页"。
    """
    if not spec:
        return list(range(1, n + 1))
    out: List[int] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                raise ValueError(f"无法解析页码区间 {part!r}（正确写法如 10-20）") from None
            if lo > hi:
                lo, hi = hi, lo
            out.extend(range(lo, hi + 1))
        else:
            try:
                out.append(int(part))
            except ValueError:
                raise ValueError(f"无法解析页码 {part!r}（正确写法如 1 或 10-20）") from None
    seen, uniq = set(), []
    for p in out:
        if 1 <= p <= n:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        elif dropped is not None:
            dropped.append(p)
    return uniq


_BAD_PREFIX_CHARS = set('<>:"/\\|?*')


def validate_prefix(prefix: str) -> Optional[str]:
    """页名前缀的安全校验；返回错误信息（None = 合法）。

    prefix 直接参与文件名拼接，必须挡掉路径分隔符与 `..`——否则
    `--prefix ../foo` 会把页图写到 `--out` 之外。
    """
    if not prefix:
        return "不能为空"
    if prefix in (".", ".."):
        return "不能是 '.' 或 '..'"
    bad = sorted(set(prefix) & _BAD_PREFIX_CHARS)
    if bad:
        return "不能包含 " + " ".join(repr(ch) for ch in bad)
    if any(ord(ch) < 32 for ch in prefix):
        return "不能包含控制字符"
    if prefix != prefix.strip():
        return "首尾不能是空白"
    if prefix.endswith("."):
        return "不能以 '.' 结尾"
    return None


# ---------------------------------------------------------------------------
# ④ manifest 读写与身份校验（一致性层的核心）
# ---------------------------------------------------------------------------
class ManifestError(Exception):
    """manifest 无法解析。**必须 fail closed**——静默当成"没有 manifest"是本层最危险的失败模式。"""


def load_manifest(path: Path) -> dict:
    """读 manifest；结构非法一律抛 ManifestError（绝不返回"空 manifest"）。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ManifestError(f"读取失败：{type(e).__name__}: {e}") from e
    try:
        obj = json.loads(raw)
    except Exception as e:
        raise ManifestError(f"JSON 解析失败：{type(e).__name__}: {e}") from e
    if not isinstance(obj, dict):
        raise ManifestError(f"顶层不是对象，而是 {type(obj).__name__}")
    pages = obj.get("pages", [])
    if not isinstance(pages, list):
        raise ManifestError("pages 字段不是数组")
    for it in pages:
        if not isinstance(it, dict) or not isinstance(it.get("name"), str):
            raise ManifestError("pages 里存在非法条目（缺 name）")
    return obj


def identity_mismatches(old: dict, want: dict) -> List[str]:
    """返回身份不一致项的可读描述；空 = 一致。

    ⚠️ 旧 manifest **缺字段**也算不一致：缺字段意味着"无法证明它是同一套配置"，
    这种情况继续写的后果是 manifest 替磁盘撒谎，与"多写了一页"不是一个量级。
    """
    out: List[str] = []
    for k in IDENTITY_KEYS:
        if k not in old:
            out.append(f"{k}: 旧 manifest 未记录（无法证明与本次一致）")
        elif old[k] != want[k]:
            out.append(f"{k}: 旧={old[k]!r}  本次={want[k]!r}")
    return out


def verified_entries(out: Path, old: dict) -> Tuple[Dict[str, dict], int]:
    """把旧 manifest 的条目过一遍磁盘，返回 (可信条目, 被剔除条数)。

    ⚠️ 只有"文件存在 + PNG 头可解析 + 头尺寸与记录一致"的条目才允许用来跳过。
    其余（文件被删 / 被换 / 被截断）一律剔除 —— manifest 必须反映**当前可验证的
    产物状态**，而不是历史日志。
    """
    proven: Dict[str, dict] = {}
    dropped = 0
    for it in old.get("pages") or []:
        nm = it["name"]
        wh = png_size_file(out / f"{nm}.png")
        if wh is None or (it.get("w"), it.get("h")) != wh:
            dropped += 1
            continue
        proven[nm] = it
    return proven, dropped


def inspect_existing(out: Path, manifest_path: Path, want: dict) -> Tuple[dict, Optional[str]]:
    """运行前校验既有产物。返回 (old, err)：

        err is None  → 可以继续（old = 已解析的 manifest，无 manifest 时为 {}）
        err 非空     → 调用方打印并拒绝运行（fail closed）
    """
    if manifest_path.exists():
        try:
            old = load_manifest(manifest_path)
        except ManifestError as e:
            return {}, (
                f"✗ 无法解析 {manifest_path}（{e}）\n"
                f"  没有可信的来源留痕就继续，会把来源不明的页图当成合法产物 —— 拒绝运行。\n"
                f"  请修好或删除该 manifest 后重跑（已存在的页图不会被自动信任）。"
            )
        bad = identity_mismatches(old, want)
        if bad:
            lines = [f"✗ {out} 里已有另一套配置（或另一份 PDF）的产物，拒绝续跑："]
            lines += [f"      {x}" for x in bad]
            lines += [
                f"    旧：{old.get('pdf')}",
                f"    新：{want.get('pdf')}",
                "  ⚠ --force 只重渲页图，不能改变来源或渲染配置；"
                "把它当「换来源」用会把两本书的页图混在一起。",
                f"  请改用别的 --out，或手工清空 {out}（含 {MANIFEST}）后重跑。",
            ]
            return old, "\n".join(lines)
        return old, None

    if out.is_dir():
        stray = sorted(p.name for p in out.glob("*.png"))
        if stray:
            lines = [f"✗ {out} 里有 {len(stray)} 个 PNG，但没有 {MANIFEST}："]
            lines += [f"      {nm}" for nm in stray[:5]]
            if len(stray) > 5:
                lines.append(f"      …（共 {len(stray)} 个）")
            lines += [
                "  没有 manifest 就无法证明这些页图来自哪份 PDF、什么 DPI、哪个渲染器，",
                "  盲目跳过会把不同来源的页图混在一起 —— 拒绝运行。",
                f"  请改用别的 --out，或手工清空 {out} 后重跑。",
            ]
            return {}, "\n".join(lines)

    return {}, None


def page_state(dst: Path, entry: Optional[dict], est: Tuple[int, int],
               allow_adopt: bool) -> str:
    """判定一个目标页当前处于什么状态（dry-run 与正式运行共用这一份判定）。

        'ok'       —— 文件在、PNG 头可解析、尺寸与 manifest 记录一致 → 跳过
        'adopt'    —— 文件在、头可解析、尺寸与**本次渲染预测**一致，但 manifest 没记录
                      （中断续跑留下的已完成页）→ 复核通过后补记进 manifest 并跳过
        'rerender' —— 其余一律重渲（缺失 / 截断 / 尺寸不符 / 来源无记录）

    ⚠️ `allow_adopt` 只在"目录已由一份身份匹配的 manifest 证明属于本 PDF"时为真。
    没有 manifest 时，磁盘上的 PNG 无法归属，即使尺寸对得上也不补记（尺寸不构成来源证明：
    两本同为 A4 的书在 300 DPI 下尺寸完全一样）。
    """
    wh = png_size_file(dst)
    if wh is None:
        return "rerender"
    if entry is not None:
        return "ok" if (entry.get("w"), entry.get("h")) == wh else "rerender"
    return "adopt" if (allow_adopt and wh == est) else "rerender"


def build_plan(doc, targets: List[int], out: Path, digits: int, prefix: str, dpi: int,
               proven: Dict[str, dict], force: bool,
               allow_adopt: bool) -> List[Tuple[int, str, str, Tuple[int, int]]]:
    """算出每一页的动作：(页码, 文件名, 状态, 预测尺寸)。

    ⚠️ `proven` 的键是**页名 stem**（manifest 里 `name` 字段，如 `page_001`），
    不是带 `.png` 的文件名 —— 两处必须用同一个键，否则查不到会让"已记录可跳过"
    悄悄退化成"补记"，判定全错但表面上还在正常跑。
    """
    plan = []
    for p in targets:
        stem = f"{prefix}_{p:0{digits}d}"
        est = pixmap_size(doc[p - 1], dpi)
        st = "rerender" if force else page_state(out / f"{stem}.png", proven.get(stem), est,
                                                 allow_adopt)
        plan.append((p, f"{stem}.png", st, est))
    return plan


# ---------------------------------------------------------------------------
# ⑤ 输出目录独占锁 —— 实现已上移到 `linefix.common.OutputLock`（口径只写一份）
# ---------------------------------------------------------------------------
def preview(nums: List[int], limit: int = 8) -> str:
    s = ", ".join(str(x) for x in nums[:limit])
    return s + (f" …（共 {len(nums)} 个）" if len(nums) > limit else "")


# ---------------------------------------------------------------------------
# ⑥ CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m pipeline.split_pages",
        description="拆页：PDF → 逐页 PNG（默认 300 DPI，可断点续跑）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", required=True, help="源 PDF 路径")
    ap.add_argument("--out", default=None, help="输出目录（默认 <pdf 同目录>/pages）")
    ap.add_argument("--dpi", type=int, default=DEFAULT_DPI, help=f"渲染 DPI（默认 {DEFAULT_DPI}）")
    ap.add_argument("--prefix", default=DEFAULT_PREFIX, help=f"页名前缀（默认 {DEFAULT_PREFIX}）")
    ap.add_argument("--pages", default=None, help="只渲染指定页，如 1,5,10-20")
    ap.add_argument("--limit", type=int, default=0, help="只渲染前 N 页（抽样试跑，0 = 不限制）")
    ap.add_argument("--force", action="store_true",
                    help="同一 PDF + 同一渲染配置下，强制重渲已存在的页（不会改变来源/配置）")
    ap.add_argument("--dry-run", action="store_true", help="只列出将渲染的页与尺寸，不写盘")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = build_parser().parse_args(argv)

    # ---- 参数校验（全部在动手之前，且把 traceback 变成人话）----
    pdf = Path(args.pdf)
    if not pdf.is_file():
        print(f"✗ 找不到 PDF：{pdf}")
        return 2
    if args.dpi <= 0:
        print(f"✗ --dpi 必须为正数，收到 {args.dpi}")
        return 2
    if args.limit < 0:
        print(f"✗ --limit 必须 >= 0（0 = 不限制），收到 {args.limit}")
        return 2
    prefix_err = validate_prefix(args.prefix)
    if prefix_err:
        print(f"✗ --prefix 不合法：{prefix_err}")
        return 2

    out = Path(args.out) if args.out else pdf.parent / "pages"
    if out.exists() and not out.is_dir():
        print(f"✗ --out 已存在但不是目录：{out}")
        return 2

    try:
        doc = pymupdf.open(pdf)
    except Exception as e:
        print(f"✗ 打开 PDF 失败：{type(e).__name__}: {e}")
        return 2

    try:
        return _run(args, pdf, out, doc)
    finally:
        try:
            doc.close()          # Windows 上文件锁释放更确定，长任务也不攒句柄
        except Exception:
            pass


def _run(args, pdf: Path, out: Path, doc) -> int:
    if doc.needs_pass:
        print("✗ 该 PDF 已加密，需要密码才能渲染")
        return 2
    n_total = doc.page_count
    if n_total == 0:
        print("✗ 该 PDF 没有页面")
        return 2

    dropped: List[int] = []
    try:
        targets = parse_page_spec(args.pages, n_total, dropped)
    except ValueError as e:
        print(f"✗ --pages 解析失败：{e}")
        return 2
    if args.limit:
        targets = targets[: args.limit]
    if args.pages and not targets:
        print(f"✗ --pages 在 1..{n_total} 里没有任何有效页（不做事就退出，避免看起来像成功）")
        return 2

    digits = max(3, len(str(n_total)))
    print(f"源文件   : {pdf}")
    print(f"页码     : 共 {n_total} 页 → 本次处理 {len(targets)} 页"
          f"{'（--pages 过滤）' if args.pages else ''}")
    if dropped:
        uniq = sorted(set(dropped))
        print(f"  ⚠ 忽略越界页码（有效范围 1..{n_total}）：{preview(uniq)}")
    print(f"输出     : {out}")
    print(f"渲染     : {args.dpi} DPI，页名 {args.prefix}_{1:0{digits}d}.png")

    try:
        pdf_sha = C.file_sha256(pdf)
        pdf_bytes = pdf.stat().st_size
    except OSError as e:
        print(f"✗ 读取 PDF 失败（可能在运行中被移动/删除）：{type(e).__name__}: {e}")
        return 2

    manifest_path = out / MANIFEST
    want = {
        "pdf_sha256": pdf_sha,
        "dpi": args.dpi,
        "renderer": renderer_tag(),
        "prefix": args.prefix,
        "digits": digits,
        "pdf": str(pdf),
    }

    if args.dry_run:
        # 只读：不建目录、不加锁。判定与正式运行走的是同一组函数（page_state）。
        old, err = inspect_existing(out, manifest_path, want)
        if err:
            print("\n" + err)
            return 2
        proven, n_orphan = verified_entries(out, old)
        plan = build_plan(doc, targets, out, digits, args.prefix, args.dpi, proven, args.force,
                          manifest_path.exists())
        print("\n[dry-run] 将渲染：")
        for p, name, st, (w, h) in plan:
            pg = doc[p - 1]
            note = {"ok": "（已存在且来源可证，将跳过）",
                    "adopt": "（已存在、manifest 未记录，将复核后补记并跳过）"}.get(st, "")
            print(f"  {name}   {w}×{h}px   {pg.rect.width:.1f}×{pg.rect.height:.1f}pt{note}")
        if n_orphan:
            print(f"  ⚠ manifest 里有 {n_orphan} 条记录在磁盘上对不上，不会被当作可跳过")
        return 0

    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"✗ 无法创建输出目录 {out}：{type(e).__name__}: {e}")
        return 2

    lock = C.OutputLock(out / LOCKFILE)
    if not lock.acquire():
        print(f"✗ 已有另一个 split_pages 正在操作 {out}（{LOCKFILE} 被占用）")
        print("  并发运行会让 manifest 互相覆盖（磁盘 100 页、清单只剩 50 页），请等它跑完再重试。")
        return 2
    try:
        return _render(args, pdf, out, doc, n_total, targets, digits, pdf_sha, pdf_bytes,
                       manifest_path, want)
    finally:
        lock.release()


def _render(args, pdf: Path, out: Path, doc, n_total: int, targets: List[int],
            digits: int, pdf_sha: str, pdf_bytes: int,
            manifest_path: Path, want: dict) -> int:
    # ---- 续跑一致性：先证明目录属于"这一份 PDF + 这一套配置"，再谈跳过 ----
    old, err = inspect_existing(out, manifest_path, want)
    if err:
        print("\n" + err)
        return 2

    proven, n_orphan = verified_entries(out, old)
    plan = build_plan(doc, targets, out, digits, args.prefix, args.dpi, proven, args.force,
                      manifest_path.exists())
    n_skip_preexisting = sum(1 for _, _, st, _ in plan if st == "ok")

    per_page: List[dict] = []      # 本次真正渲染出来的
    adopted: List[dict] = []       # 磁盘已有、manifest 未记录，复核后补认的
    failed: List[int] = []
    n_done = n_fail = n_size_drift = 0
    interrupted = False
    t0 = time.time()
    total = len(plan)

    try:
        for k, (p, name, st, est) in enumerate(plan, 1):
            dst = out / name
            if st == "ok":
                print(f"[{k}/{total}] {name}: 已存在且来源可证，跳过")
                continue
            if st == "adopt":
                wh = png_size_file(dst) or est
                try:
                    nbytes = dst.stat().st_size
                except OSError:
                    nbytes = 0
                adopted.append({"name": dst.stem, "page": p, "w": wh[0], "h": wh[1],
                                "bytes": nbytes, "sec": 0.0})
                print(f"[{k}/{total}] {name}: 已存在但 manifest 未记录，尺寸复核通过 → 补记后跳过")
                continue
            try:
                t = time.time()
                data = render_page(doc[p - 1], args.dpi)
                C.atomic_write_bytes(dst, data)      # 原子落盘（口径见 linefix.common）
                sec = time.time() - t
            except Exception as e:
                # ⚠️ 失败不进 per_page：manifest 不能声称"这页本次是好的"。
                #    旧记录若仍对应磁盘上完好的文件，会在下面按磁盘实况保留。
                n_fail += 1
                failed.append(p)
                print(f"[{k}/{total}] {name}: ✗ 渲染失败 {type(e).__name__}: {e}")
                continue
            # 尺寸以**实际产出的 PNG 头**为准；与预测公式不符就报出来（防 pymupdf 行为变更）
            actual = png_size(data)
            est_check = pixmap_size(doc[p - 1], args.dpi)
            if actual is None or actual != est_check:
                n_size_drift += 1
                print(f"[{k}/{total}] {name}: ⚠ 尺寸与预测不符 实际={actual} 预测={est_check}")
            w, h = actual if actual else est_check
            per_page.append({"name": dst.stem, "page": p, "w": w, "h": h,
                             "bytes": len(data), "sec": round(sec, 3)})
            n_done += 1
            print(f"[{k}/{total}] {name}: {w}×{h}px  {len(data)/1e6:.1f}MB  {sec:.2f}s")
    except KeyboardInterrupt:
        # 中途 Ctrl+C：**不要把已完成的部分丢掉**。照常往下走完 merge + 写 manifest，
        # 这样下一次运行能凭 manifest 正确续跑（而不是因为"没 manifest 却有 PNG"被拒绝）。
        interrupted = True
        print("\n⚠ 收到中断：已完成的页会照常写进 manifest，下次可断点续跑")

    elapsed = time.time() - t0

    # ---- 重建 manifest：只保留"磁盘上确实存在、且尺寸与记录自洽"的条目 ----
    # 这样 manifest 反映的是当前可验证的产物状态，而不是历史日志：
    #   · 文件被手工删掉 → 条目剔除
    #   · 文件被换掉/截断 → 条目剔除
    #   · 本次重渲失败但旧文件仍完好 → 旧条目保留（它确实是这份 PDF 的产物）
    merged: Dict[str, dict] = dict(proven)
    for x in per_page + adopted:
        merged[x["name"]] = x

    final_pages: List[dict] = []
    n_stale = 0
    for nm, it in merged.items():
        wh = png_size_file(out / f"{nm}.png")
        if wh is None or (it.get("w"), it.get("h")) != wh:
            n_stale += 1
            continue
        final_pages.append(it)
    final_pages.sort(key=lambda x: x["name"])
    final_names = {x["name"] for x in final_pages}

    doc_out = {
        "pdf": str(pdf),
        "pdf_bytes": pdf_bytes,
        "pdf_sha256": pdf_sha,
        "dpi": args.dpi,
        "renderer": want["renderer"],
        "prefix": args.prefix,
        "digits": digits,
        "page_count": n_total,
        # ⚠️ 语义：manifest 当前持有多少条**经磁盘复核**的页记录（== 可信页图数），
        #    不是"本次渲染了几页"。本次渲染数见下面的 stdout 汇总。
        #    旧名 rendered 容易读成"本次渲了 N 页"，这里保留字段名以兼容既有消费方。
        "rendered": len(final_pages),
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pages": final_pages,
    }
    C.atomic_write_text(manifest_path, json.dumps(doc_out, ensure_ascii=False, indent=1))

    render_sec = sum(x["sec"] for x in per_page)
    total_mb = sum(x["bytes"] for x in per_page) / 1e6
    n_skip = n_skip_preexisting + len(adopted)
    head = "INTERRUPTED" if interrupted else "DONE"
    print(f"\n{head} 渲染 {n_done} 页｜补记 {len(adopted)}｜跳过 {n_skip}｜失败 {n_fail}｜耗时 {elapsed:.1f}s")
    if n_done:
        print(f"  本次产出 {total_mb:.1f}MB，页均 {render_sec/n_done:.2f}s（只按成功渲染的页计）")
    print(f"  页图   -> {out}")
    print(f"  留痕   -> {manifest_path}（{len(final_pages)} 页可追溯）")
    # 目录里存在、但 manifest 无法背书的 PNG：它们会被下游 glob(page_*.png) 一起吃掉，
    # 所以必须明确报出来，不能让它安静地留在那儿。
    try:
        stray = sorted(p.name for p in out.glob("*.png") if p.stem not in final_names)
    except OSError:
        stray = []
    if stray:
        print(f"  ⚠ {out} 里还有 {len(stray)} 个 PNG 不在 manifest 覆盖范围内（来源不明）：{preview(stray)}")
    if n_orphan:
        print(f"  ⚠ manifest 里有 {n_orphan} 条记录在磁盘上对不上（被删/被换/被截断），已剔除")
    if n_stale:
        print(f"  ⚠ 提交前又有 {n_stale} 条记录在磁盘上失效，已剔除")
    if len(adopted):
        print(f"  ⚠ {len(adopted)} 页是「磁盘已有但 manifest 未记录」（上次中断的残留）："
              f"PNG 头尺寸与本次渲染预测一致，已复核后补记")
    if n_size_drift:
        print(f"  ⚠ 有 {n_size_drift} 页的尺寸与预测公式不符（清单已按 PNG 实际尺寸记录）"
              f" —— 若成片出现，说明 pymupdf 取整行为变了，请复核 pixmap_size()")
    if n_fail:
        missing = [p for p in failed
                   if f"{args.prefix}_{p:0{digits}d}" not in final_names]
        print(f"  ⚠ 有 {n_fail} 页渲染失败：{preview(failed)}")
        if missing:
            print(f"    其中 {len(missing)} 页没有任何可用页图（会从 manifest 里缺席）：{preview(missing)}")
        print("    这些页在后续产物里会缺席，请先修好再往下走")
    if interrupted:
        return 130
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
