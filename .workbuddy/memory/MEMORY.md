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
- 🆕 **`lines[].role`**（`PRODUCT_SCHEMA` **6**）：`"text"` / `"page_number"`。印刷页码行
  **只打标记、不删除** —— 删了漏行审计会把页码框误报成「有框无行」。判据
  `linefix.common.page_number_role()` **只写一份**：整行 1~4 位纯数字 ∧ `y/页高 > 0.85`。
  全书实测 **363 行**判为页码；🔴 **正文区另有 4 行纯数字不是页码**
  （`page_336` 的 `99` 其实是 `——”` 被误识）→ 别按「整行是数字」自己过滤。
  下游取正文：`[l for l in lines if l["role"] != "page_number"]`。
- 跑单页：`--pages-filter page_150 --force`。已有产物补 `role` 用
  `scripts/backfill_line_role.py`（纯函数补写，已验证与真正重跑**逐字段一致**）。
- 评测脚本也读同一份判据；页高改成逐页读 PNG 头（**不再硬编码 2335**）。
- 版本闸门 `SUPPORTED_RAPIDOCR = ("3.9.2",)`：hook 的是 rapidocr 内部 API，未验证版本 exit 2。
- 埋点（`--alts` 默认开）**不**降级：钩子装不上 → exit 1，要关必须显式 `--no-alts`。
- 口径指纹（`check_reusable`）含：runtime 版本 + 权重实际 SHA256 + backend（含 `--backend` 覆盖）
  + det_boxes_source + `product_schema` + `hook_schema` + top2 口径 + 关键配置键。
- 并发：`<out>/.ocr.lock`。
- ⚠️ 干净页面上绝大多数位置的 p2/p1 < `TOP2_FLOOR`(1e-3)，top2 稀疏是**预期**，不是没采到。

## linefix 层的分工
- `linefix/common.py` = **产线各阶段共用的实现层**（原子写 / 产物 JSON / 页图身份 / 引擎构造 /
  缓存指纹 / `OutputLock` / 几何）。改动要在所有阶段重跑回归。
- 纠错层（`audit` / `rescue` / `recheck`）**尚未移植**，需要时再加。
- 🔴 `fingerprint_drift()` **fail closed**：`want` 里只要有 `_` 前缀键（如 `_cfg_error`）
  一律返回非空差异。"指纹算不出来"绝不能被读成"指纹一致"。
- `ocr_fingerprint` 的 backend 记三个键：`backend_config` / `backend_override` / `backend`（生效）。
- 🔴 `atomic_write_bytes` 临时名 = `.{名}.{pid}.{tid}.tmp`（唯一、隐藏）。
  **Windows 实测**：`os.replace` 撞上"目标被别的句柄打开"会抛 `PermissionError`
  （写侧已有界重试 5×20ms）；反向地，写方 replace 的一瞬间**读方 open 也会被拒**
  ⇒ 读写并发要靠输出目录锁错开，不要让每个读点自己重试。
- `image_drift()` 返回 `None` **只**表示"比过了且一致"；页图不存在返回 `"页图不存在：..."`。
- `is_figure_page()` 返回 `Optional[bool]`：`None` = **判不了**（不是"确定不是图版页"）。
- `dumps_product_json()` 末尾有**逐值自校验**（哨兵替换是格式 hack，对不上就拒绝落盘）。

## 全盘运行（已验证的书）
- `orign_pdf/围城.pdf` 368 页 / 300dpi → 1632×2335。
- 拆页 `python -m pipeline.split_pages --pdf orign_pdf/围城.pdf`（约 47s / 56MB）。
- OCR `python -m pipeline.ocr --pages orign_pdf/pages`（**正文页约 8.6~9.5s/页**，全书约 58 分钟）。
  ⚠️ 必须 `--max-minutes 9` 分批续跑：**单次后台调用超过约 10 分钟会被静默回收**。
- 全书对账结果（368 页）：9585 行 / 9587 det 框 / 差 2；三条不变式（bbox↔det_index、
  det_index 划分、dropped bbox）**全部 0 不符**；异常计数全 0。
- `orign_pdf/围城.txt` 是**原文对照文本**，抽检 93.1% 命中，但**版次不同**
  （PDF=2005 重印 ISBN 7-02-002475-0，txt=2017 ISBN 978-7-02-012789-4）→
  前置页（版权/CIP）对不上，做逐字评测要排除。
- 已知薄弱位（真实书实测）：**引号 `”`、单个数字/符号**（8 条低 conf 里占 6 条）。

## 评测（`scripts/ocr_accuracy_report.py` → `docs/ocr-accuracy-围城.md`）
- 三桶：**正文页 313** / 脚注页 46 / 前置·后置页 5（另 4 页锚定失败）。
- 正文页：**N1 剔页码 0.500%（99.500%）**｜N2 0.438%｜**汉字-only 0.143%（99.857%）**。
  逐页中位 0.286% / 90 分位 1.094% / 最差 2.455%（`page_285`），**无页 > 3%**。
- 🔴 对齐**不能用**「页内中段探针反推页首」：反推式隐含「OCR 偏移 == 参考偏移」，
  丢字就偏右 → 把本页文字判成「插入」（`page_093` 偏 2 字、`page_285` 偏 10 字）。
  正确做法：**页面开头 48 字直接锚定**（精确→模糊→吸附）+ **终点 = 下一页起点**；
  4 项自检（锚定方式 / 起点单调性 / 平移敏感度 / 参考段长 vs OCR 页长）。
- 🔴 差异归因（1057 字符）：**整段删除 644（`—` 占 472）** + 汉字↔汉字 182 + 整段插入 24。
  破折号 638 组 `——` 只剩 244 组、单 `—` 229 个 → **CTC 重复字符塌缩，结构性缺陷**。
  引号丢失 164（`‘`106 / `”`36 / `’`22），而 `“` 完全无损 → 是「小标记 + 行首尾位置」问题。
- 对照 txt 与 PDF **版次不同**（2005 重印 vs 2017）+ 脚注编码不同 → 这两类页必须单列。
- ⚠️ `docs/archive/ocr-accuracy-围城-v1.md` 是**已作废**的历史快照，别引用其数字。

## 下一步（尚未做）
- P1 **破折号修复**：规范化规则（中文规范即 `——`，本书单 `—` 仅 6/1282）
  or CTC 列宽证据（`char_x_spans_from_cols`）or 换 attention decoder —— **待选**。
- P2 引号：det 框外扩 + 首末行 padding 二次识别 / 配对状态机。
- P3 脚注页 46 页整桶排除使覆盖率仅 85% → 市面用**元素级**评测可提到 ~97%。
- P4 形近字 182（`己`↔`已` 等）：⚠️ MacBERT4CSC 已证伪（净损 −660），方向是
  LLM 后纠（只裁决"提议改动"、按阈值批量）或混淆集 + 语言模型重打分。
- 纠错层 `linefix/{audit,rescue,recheck}.py` **尚未移植**。

## 例行核验
- 环境：`.venv/Scripts/python.exe scripts/check_rapidocr_env.py`（35 项）
- 共用层：`.venv/Scripts/python.exe .tmp/test_common_review.py`（73 项）
- 拆页：`.venv/Scripts/python.exe .tmp/test_split_pages_matrix.py`（83 项）
- OCR：`.venv/Scripts/python.exe .tmp/test_ocr_review.py`（98 项）
- 评测报告：`.venv/Scripts/python.exe scripts/ocr_accuracy_report.py`
- 新对话接手入口：**`docs/HANDOFF.md`**（现状 / 待决策 / 坑点 / 文件地图）




