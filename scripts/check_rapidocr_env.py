# -*- coding: utf-8 -*-
"""RapidOCR + OpenVINO 环境核验（分 5 段断言，任一段失败即 exit 1）。

用法：
    .venv/Scripts/python.exe scripts/check_rapidocr_env.py

核验目标（针对本项目约定：rapidocr 3.9.2 + OpenVINO + PP-OCRv6 medium + use_cls=false）：
  1. 安装位置 —— 解释器与 rapidocr 包都在项目 .venv 内；版本 == 3.9.2；onnxruntime 未安装
  2. 配置解析 —— Det/Cls/Rec 的 engine_type / model_type / ocr_version；Global.use_cls
  3. 引擎实证 —— 看实际实例化的类，而不是配置里写的值
  4. 权重完整性 —— 用引擎自己的解析函数复算路径，再按 SHA256 核对
  5. 端到端 —— 合成中文图跑一遍，检查 txts / scores 命中关键词，打印 init / infer 耗时
"""

from __future__ import annotations

import hashlib
import importlib.metadata as md
import importlib.util
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENV_ROOT = PROJECT_ROOT / ".venv"
CONFIG_PATH = PROJECT_ROOT / "configs" / "rapidocr_openvino_ppocrv6_medium.yaml"

TARGET_RAPIDOCR = "3.9.2"
EXPECTED = {
    "Det": ("openvino", "medium", "PP-OCRv6"),
    "Cls": ("openvino", "mobile", "PP-OCRv4"),
    "Rec": ("openvino", "medium", "PP-OCRv6"),
}

_checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _checks.append((name, bool(ok), detail))
    flag = "PASS" if ok else "FAIL"
    print(f"  [{flag}] {name}" + (f" :: {detail}" if detail else ""))
    return bool(ok)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 1. 安装位置
def check_install_location() -> None:
    section("1. 安装位置 / 版本 / 后端排他性")

    exe = Path(sys.executable).resolve()
    check("解释器位于项目 .venv 内", exe.is_relative_to(VENV_ROOT.resolve()), str(exe))

    import rapidocr

    pkg = Path(rapidocr.__file__).resolve().parent
    check("rapidocr 包位于项目 .venv 内", pkg.is_relative_to(VENV_ROOT.resolve()), str(pkg))

    ver = md.version("rapidocr")
    check(f"rapidocr 版本 == {TARGET_RAPIDOCR}", ver == TARGET_RAPIDOCR, f"实际 {ver}")

    spec = importlib.util.find_spec("onnxruntime")
    check(
        "onnxruntime 未安装（证明无回退空间，真的走 openvino）",
        spec is None,
        "已安装 => 可能被静默回退" if spec else "未安装",
    )
    check("openvino 已安装", importlib.util.find_spec("openvino") is not None, md.version("openvino"))


# ---------------------------------------------------------------- 2. 配置解析
def check_config(ocr) -> None:
    section("2. 配置解析（配置文件里写了什么）")

    check("配置文件存在", CONFIG_PATH.is_file(), str(CONFIG_PATH))
    check("Global.use_cls == false", ocr.cfg.Global.use_cls is False, repr(ocr.cfg.Global.use_cls))
    check("Global.use_det / use_rec == true", bool(ocr.cfg.Global.use_det) and bool(ocr.cfg.Global.use_rec))

    root_dir = Path(str(ocr.cfg.Global.model_root_dir)).resolve()
    check("model_root_dir 落在 .venv 内（权重随项目自包含）", root_dir.is_relative_to(VENV_ROOT.resolve()), str(root_dir))

    for section_name, (eng, mtype, ocrev) in EXPECTED.items():
        cfg = getattr(ocr.cfg, section_name)
        check(f"{section_name}.engine_type == {eng}", cfg.engine_type.value == eng, cfg.engine_type.value)
        check(f"{section_name}.model_type == {mtype}", cfg.model_type.value == mtype, cfg.model_type.value)
        check(f"{section_name}.ocr_version == {ocrev}", cfg.ocr_version.value == ocrev, cfg.ocr_version.value)


# ---------------------------------------------------------------- 3. 引擎实证
def check_engine_class(ocr) -> None:
    section("3. 引擎实证（实际实例化的类，而非配置值）")

    for attr, label in (("text_det", "Det"), ("text_cls", "Cls"), ("text_rec", "Rec")):
        obj = getattr(ocr, attr, None)
        check(f"{label} 组件已构造", obj is not None)
        if obj is None:
            continue
        engine = getattr(obj, "session", None)
        mod = type(engine).__module__ if engine is not None else "<none>"
        cls = type(engine).__name__ if engine is not None else "<none>"
        check(
            f"{label} 实际引擎来自 rapidocr.inference_engine.openvino",
            mod == "rapidocr.inference_engine.openvino.main",
            f"{mod}.{cls}",
        )


# ---------------------------------------------------------------- 4. 权重完整性
def check_weights(ocr) -> None:
    section("4. 权重完整性（路径复算 + SHA256）")

    from rapidocr.inference_engine.base import FileInfo, InferSession

    root = Path(str(ocr.cfg.Global.model_root_dir))
    for section_name in ("Det", "Cls", "Rec"):
        cfg = getattr(ocr.cfg, section_name)
        # 注意：use_cls=false 时 TextClassifier 仍会被构造并加载权重，所以 Cls 同样必须核对
        info = InferSession.get_model_url(
            FileInfo(
                engine_type=cfg.engine_type,
                ocr_version=cfg.ocr_version,
                task_type=cfg.task_type,
                lang_type=cfg.lang_type,
                model_type=cfg.model_type,
            )
        )
        path = root / Path(info["model_dir"]).name
        if not check(f"{section_name} 权重文件存在", path.is_file(), path.name):
            continue
        actual = sha256_of(path)
        expect = info.get("SHA256")
        size_mb = path.stat().st_size / 1024 / 1024
        check(
            f"{section_name} SHA256 匹配官方清单",
            expect is not None and actual == expect,
            f"{size_mb:.1f} MB, sha256={actual[:16]}...",
        )


def find_cjk_font() -> Path | None:
    """在常见位置找一个中文字体；找不到返回 None（端到端段跳过，不算失败）。

    ⚠️ 不能写死 `C:/Windows/Fonts/msyh.ttc`：那只是"本机恰好有"，换台 Windows
    （或精简版系统）就可能没有，自检不该因此报 FAIL —— 字体不是本项目的依赖。
    """
    candidates = [
        "C:/Windows/Fonts/msyh.ttc",      # 微软雅黑
        "C:/Windows/Fonts/simhei.ttf",    # 黑体
        "C:/Windows/Fonts/simsun.ttc",    # 宋体
        "C:/Windows/Fonts/Deng.ttf",      # 等线
    ]
    for c in candidates:
        p = Path(c)
        if p.is_file():
            return p
    return None


# ---------------------------------------------------------------- 5. 端到端
def check_end_to_end() -> None:
    section("5. 端到端（合成中文图 -> det -> rec）")

    from PIL import Image, ImageDraw, ImageFont

    font_path = find_cjk_font()
    if font_path is None:
        print("  [SKIP] 未找到中文字体，跳过端到端段（字体不是本项目的依赖）")
        return
    print(f"  使用字体: {font_path}")

    lines = ["爱尔兰神话全书", "OCR 环境核验 PP-OCRv6 medium", "检测与识别均使用 OpenVINO"]
    img = Image.new("RGB", (900, 240), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(str(font_path), 40)
    for i, text in enumerate(lines):
        draw.text((30, 25 + i * 70), text, fill="black", font=font)

    tmp = Path(tempfile.gettempdir()) / "rapidocr_env_check.png"
    img.save(tmp)

    from rapidocr import RapidOCR

    t0 = time.perf_counter()
    ocr = RapidOCR(config_path=str(CONFIG_PATH))
    t_init = time.perf_counter() - t0

    result = ocr(str(tmp))
    t_infer = time.perf_counter() - t0 - t_init

    txts = list(getattr(result, "txts", None) or [])
    print(f"  识别行数: {len(txts)}")
    for t, s in zip(txts, list(getattr(result, "scores", None) or [])):
        print(f"    - {t!r}  conf={s:.4f}")

    joined = "".join(txts)
    check("识别出 3 行", len(txts) == 3, f"{len(txts)} 行")
    check("命中关键词『爱尔兰神话全书』", "爱尔兰神话全书" in joined)
    check("命中关键词『OpenVINO』", "OpenVINO" in joined)
    check("平均置信度 >= 0.9", bool(result.scores) and sum(result.scores) / len(result.scores) >= 0.9)

    elapse = getattr(result, "elapse_list", None)
    print(f"  init {t_init:.2f}s | det+rec {t_infer:.2f}s | elapse_list={elapse}")


def main() -> int:
    print(f"项目根目录: {PROJECT_ROOT}")
    print(f"解释器: {sys.executable}")

    check_install_location()

    if not CONFIG_PATH.is_file():
        print("\n配置文件缺失，后续段落跳过。")
        return 1

    from rapidocr import RapidOCR

    # 首次初始化会联网下载 PP-OCRv6 medium（det≈59MB / rec≈73MB）并编译
    print("\n(初始化引擎，首次会下载并编译 medium 权重，请稍候…)")
    t0 = time.perf_counter()
    ocr = RapidOCR(config_path=str(CONFIG_PATH))
    print(f"  引擎初始化完成，用时 {time.perf_counter() - t0:.2f}s")

    check_config(ocr)
    check_engine_class(ocr)
    check_weights(ocr)
    check_end_to_end()

    failed = [name for name, ok, _ in _checks if not ok]
    print("\n" + "=" * 60)
    print(f"合计 {len(_checks)} 项，通过 {len(_checks) - len(failed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：")
        for name in failed:
            print(f"  - {name}")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
