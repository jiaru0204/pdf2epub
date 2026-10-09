# pdf2epub 项目长期约定

## 目录（先看这条，曾经踩过）
- **本项目 = `D:\Project\pdf2epub`**（IDE 工作区）。
- `D:\pdf_to_epub` = **测试/参考项目**，只做参考，**不要改它**。
- `D:\pdf2epub` = 无关的旧目录。
- 三者是磁盘上独立目录，没有 junction/symlink。⚠️ 别再把改动写进参考项目。

## OCR 环境
- 虚拟环境固定为项目内 **`.venv`**（不是 `.env`）。解释器 `D:\python\Python\python.exe`，Python 3.13.15。
- 技术栈固定为 **rapidocr 3.9.2 + OpenVINO 后端 + PP-OCRv6 medium**（det/rec 均 medium）。
- **禁止安装 `onnxruntime`** —— 保持「未安装」状态，才能证明推理确实走 openvino（装了就有静默回退空间）。
- 配置文件：`configs/rapidocr_openvino_ppocrv6_medium.yaml`。
  铁律：必须以官方 `site-packages/rapidocr/config.yaml` 为基线**整体复制**再改
  （`ParseParams.load` 是整体替换语义，只写片段必然 `KeyError`）。
- `use_cls: false`（禁用方向分类），但 **`Cls.engine_type` 仍必须指向 openvino** ——
  `TextClassifier` 无条件被构造，不改就报 `ImportError: onnxruntime is not installed`。
  `Cls.ocr_version` 只能是 `PP-OCRv4`/`PP-OCRv5`，填 `PP-OCRv6` 会 `KeyError`。
- 权重落点：`.venv/Lib/site-packages/rapidocr/models/`（`Global.model_root_dir: null` 即包内），随项目自包含。
- 生产环境**复用同一个 `RapidOCR` 实例**：openvino 初始化约 1s（首次含编译约 7.5s），不要每条文本重建。

## 拆页（产线第一环）`pipeline/split_pages.py`
- 调用：`python -m pipeline.split_pages --pdf X` → `<pdf 同目录>/pages/page_NNN.png` + `split_manifest.json`。
- 🔴 **核心不变式**：跳过一页 = manifest 有该页记录 ∧ 身份一致 ∧ PNG 头尺寸与记录自洽。
  「PNG 存在」单独**不是**跳过理由。
- 身份键 `(pdf_sha256, dpi, renderer, prefix, digits)`，任一不一致（含旧 manifest 缺字段）→ exit 2。
- `--force` **只**表示「同来源同配置重渲」；不能换 PDF、不能改配置。**没有 `--reset-output`**（刻意不加）；
  要换来源就换 `--out` 或手工清空目录。
- fail-closed：manifest 损坏 → exit 2；目录有 PNG 但无 manifest → exit 2。
- `KeyboardInterrupt` 会照常写 manifest（返回 130），续跑靠按预测尺寸复核后补记（adopt）。
- 并发：`<out>/.split_pages.lock`（OS advisory lock，进程退出自动释放）。
- ⚠️ 换 pymupdf 版本必须重跑回归矩阵（`pixmap_size()` 依赖其 ceil 取整行为）。
- `linefix/common.py` 当前**只有本阶段要用的 3 个函数**；别预先塞没人调用的接口。

## 例行核验
- 环境：`.venv/Scripts/python.exe scripts/check_rapidocr_env.py`（35 项）
- 拆页回归：`.venv/Scripts/python.exe .tmp/test_split_pages_matrix.py`（83 项，覆盖 review 的测试矩阵）

