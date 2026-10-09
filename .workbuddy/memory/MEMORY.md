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

## OCR（产线第二环）`pipeline/ocr.py`
- 调用：`python -m pipeline.ocr --pages <页图目录>` → `<pages>/../textlines/*.textlines.json` + `ocr_manifest.json`。
- 🔴 **最重要的口径：`det_index` 是证据归属的唯一依据**。
  `det_boxes[k]` ↔ `lines[].det_index == k` ↔ `dropped_lines[].det_index == k` 是**同一个 det 框**。
  实现方式：在 `TextRecognizer.__call__` 处**复现引擎自己的 `argsort(width_list)` 分批**
  （crop 顺序 = det 顺序），拿到「批次位置 → det 下标」。
  **绝不用文本多重集反查** —— 同一页出现两次相同文本时那样只能猜，等于把 A 行的证据挂到 B 行的框上。
  每页断言 `len(kept) == len(lines)`，不等就抛错（记 `top2_error`），不退回文本匹配。
- 🔴 **验证不变式**：`lines[m].bbox == det_boxes[lines[m].det_index]`
  （过滤只删不重排不改坐标，所以必须逐值相等；下标有偏差必然失败）。
- `det_boxes` = **过滤前的原始 det 框**。`result.boxes` 与 `result.txts` 永远 1:1，**不能**当原始框用。
- `provenance.audit_capable=False` 表示这是 `--allow-filtered-det` 降级产物，下游**必须拒绝审计**。
- 版本闸门 `SUPPORTED_RAPIDOCR = ("3.9.2",)`：hook 的是 rapidocr 内部 API，未验证版本 exit 2。
- 埋点（`--alts` 默认开）**不**降级：钩子装不上 → exit 1，要关必须显式 `--no-alts`。
- 口径指纹（`check_reusable`）含：runtime 版本 + 权重实际 SHA256 + backend（含 `--backend` 覆盖）
  + det_boxes_source + `product_schema` + `hook_schema` + top2 口径 + 关键配置键。
- 并发：`<out>/.ocr.lock`。
- ⚠️ 干净页面上绝大多数位置的 p2/p1 < `TOP2_FLOOR`(1e-3)，top2 稀疏是**预期**，不是没采到。

## linefix 层的分工
- `linefix/common.py` = **产线各阶段共用的实现层**（原子写 / 产物 JSON / 页图身份 / 引擎构造 /
  缓存指纹 / `OutputLock` / 几何）。改动要在两个阶段都重跑回归。
- 纠错层（`audit` / `rescue` / `recheck`）**尚未移植**，需要时再加。

## 例行核验
- 环境：`.venv/Scripts/python.exe scripts/check_rapidocr_env.py`（35 项）
- 拆页：`.venv/Scripts/python.exe .tmp/test_split_pages_matrix.py`（83 项）
- OCR：`.venv/Scripts/python.exe .tmp/test_ocr_review.py`（98 项）


