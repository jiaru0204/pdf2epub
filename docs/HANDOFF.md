# 交接文档（2026-10-09 深夜）

> 用途：给**新对话**快速接手。看完这一份 + 项目记忆 `.workbuddy/memory/MEMORY.md` 就能继续干。
> 当前 HEAD：`dccfefd`（工作区干净）

---

## 0. 一句话现状

产线前两环（拆页 → OCR）在《围城》368 页真实书上**全盘跑通并逐页对账通过**；
准确率评测做完了（正文页 N1 **99.500%**、汉字 **99.857%**）；
**下一步是修产物缺陷**（破折号 → 引号），纠错层（audit/rescue/recheck）尚未移植。

---

## 1. 新对话开场提示词（可直接粘贴）

```
工作目录 D:\Project\pdf2epub。先读 docs/HANDOFF.md 和 .workbuddy/memory/MEMORY.md，
然后告诉我当前状态和你建议的下一步。
```

---

## 2. 项目坐标（⚠️ 曾经踩过，务必先确认）

| 路径 | 是什么 |
|---|---|
| **`D:\Project\pdf2epub`** | **本项目**（唯一要改的地方） |
| `D:\pdf_to_epub` | 测试/参考项目，**只参考，不要动** |
| `D:\pdf2epub` | 无关的旧目录 |

三者是磁盘上独立目录，没有 junction/symlink。

---

## 3. 环境与常用命令

```bash
# 解释器：项目内 .venv（不是 .env）—— rapidocr 3.9.2 + OpenVINO + PP-OCRv6 medium
.venv/Scripts/python.exe scripts/check_rapidocr_env.py        # 环境自检 35 项

# 产线第一环：拆页
.venv/Scripts/python.exe -m pipeline.split_pages --pdf orign_pdf/围城.pdf

# 产线第二环：OCR（正文页约 8.6~9.5s/页，全书约 58 分钟）
.venv/Scripts/python.exe -m pipeline.ocr --pages orign_pdf/pages --max-minutes 9

# 评测报告（读 orign_pdf/textlines/，重算 docs/ocr-accuracy-围城.md）
.venv/Scripts/python.exe scripts/ocr_accuracy_report.py

# 回归（都在 .tmp/，不进库）
.venv/Scripts/python.exe .tmp/test_common_review.py           # 73 项
.venv/Scripts/python.exe .tmp/test_split_pages_matrix.py      # 83 项
.venv/Scripts/python.exe .tmp/test_ocr_review.py              # 98 项
```

🔴 **单次后台调用超过约 10 分钟会被静默回收** → OCR 必须 `--max-minutes 9` 分批续跑。

---

## 4. 链路状态

| 环 | 文件 | 状态 |
|---|---|---|
| ① 拆页 | `pipeline/split_pages.py` | ✅ 跑通（47s / 56MB） |
| ② OCR | `pipeline/ocr.py` | ✅ 跑通（368/368 页有产物） |
| ③ 评测 | `scripts/ocr_accuracy_report.py` → `docs/ocr-accuracy-围城.md` | ✅ 已完成（含对齐质量自检） |
| ④ 纠错层 | `linefix/{audit,rescue,recheck}.py` | ❌ **尚未移植** |

共用实现层 `linefix/common.py` 已对齐参考版完整实现（判据 / 几何 / 图像 IO / 产物读写 /
证据归一化 / 字级定位 / 引擎构造与指纹）。

### 全书对账（368 页，验收依据）
- 9585 行 / 9587 det 框 / 差 2（两条漏行候选：`page_163` `page_262`）
- 三条不变式**全部 0 不符**：`lines[].bbox == det_boxes[det_index]`、
  `lines ∪ dropped 的 det_index` 恰好划分全部框、`dropped_lines[].bbox` 对上 `det_boxes`
- 异常计数全 0：`top2_error` / `alignment_failed` / `drop_selfcheck drift` / `audit_capable=False`

---

## 5. 评测报告（`docs/ocr-accuracy-围城.md`）现状

### 分桶
| 桶 | 页数 | 说明 |
|---|---|---|
| **正文页** | **313** | 主口径 = 剔除脚注页与前置页 |
| 脚注页 | 46 | 对照 txt 的脚注编码与书上不同，整桶排除 |
| 前置/后置页 | 5 | 与对照文本版次不同 |
| 锚定失败 | 4 | 封面/插页 |

### 指标（正文页）
| 口径 | CER | 准确率 |
|---|---|---|
| N1（含页码行） | 0.891% | 99.109% |
| **N1（剔页码行）** | **0.500%** | **99.500%** |
| N2（NFKC + 剔页码） | 0.438% | 99.562% |
| **汉字-only** | **0.143%** | **99.857%** |

逐页：中位 0.286% / 90 分位 1.094% / 最差 2.455%（`page_285`）；272/313 页 < 1%，**无页 > 3%**。

### 差异归因（正文页，1057 字符）
| 类型 | 字符数 |
|---|---|
| **整段删除 644**（其中破折号 `—` **472**） | 60.9% |
| 汉字↔汉字（真错字） | 182 |
| 整段插入 | 24 |
| 标点样式（全角括号 50 / 分号 12 / 叹号 7 / 逗号 6） | 75 |
| 其他（「其他」64 + 汉字↔非汉字 35 + 引号样式 27 + 破折号样式 5 + 冒号 1） | 132 |
| **合计** | **1057** |

### 漏字落在行内什么位置（我另外算过，报告里还没有）
| 位置 | 字符 | 占比 | 主要成分 |
|---|---|---|---|
| 行中 | 580 | 64.3% | `—` 299、`‘` 63 |
| 行首（前 2 字内） | 235 | 26.1% | `—` 143、`”` 36 |
| 行尾（后 3 字内） | 87 | 9.6% | `—` 28、`‘` 12 |

⇒ **丢的几乎全是标点，`—` 一个就占 516/902 = 57%。**

---

## 6. 待办与待决策（**这是新对话最该看的部分**）

### 🔴 P1 破折号 `——` → `—`（收益最大）
**现象**：对照 txt `—` 1282 个 = **638 组 `——`** + 6 个单 `—`；
OCR 产物 717 个 = 244 组 `——` + **229 个单 `—`** ⇒ **394 组被吞成一个字符**。

**机制**（已确认，非猜测）：rec 用 CTC 解码，CTC 的「重复 token 合并」要求两个相同字符
**中间必须出现 blank** 才会输出两次；纸上 `——` 是一条连续长横线，blank 大概率不触发 → 塌缩。
**结构性缺陷，调阈值解决不了。** det 侧无责（无单独长横线框被丢）。

**三条候选路线（待选）**：
1. **规范化规则**（快、风险低）：中文规范 GB/T 15834 破折号即占两字位的 `——`，
   本书单 `—` 仅 6/1282 → 「行内孤立 `—` 补成 `——`」误伤风险近零。
2. **CTC 列宽证据**（保守、要动字级定位）：用 `char_x_spans_from_cols` 判 `—` 是否占 ~2 字宽。
3. **换解码器**（治本、成本高）：attention/Transformer decoder 的识别模型没有 blank 分隔问题。

### 🟠 P2 引号丢失
`‘` 106 / `”` 36 / `’` 22 被删（合计 164）。而 `“` 2706→2706 **完全无损** →
说明是「**小标记 + 行首/行尾位置**」问题，不是「引号识别」问题。
市面做法：det 框水平外扩 + 首末行 padding **二次识别**；或引号配对状态机后处理。

### 🟠 P3 评测覆盖率只有 85%
46 页脚注页整桶排除。市面基准（OmniDocBench / olmOCR-Bench / CC-OCR）用**元素级**评测
——把脚注正文部分照算、脚注标记单列，覆盖率可提到约 97%。**要动评测脚本结构，待定。**

### 🟡 P4 汉字形近字 182
`己`↔`已`、`刺`↔`剌`、`闾`↔`间`、`伕`↔`侠`、`邝`↔`邝`…
⚠️ 通用纠错模型（MacBERT4CSC）**已在本项目被证伪**（净损 −660：改对 10 / 误改 ~670）。
方向应为：LLM 后纠（按阈值批量、只裁决"提议改动"条目）或混淆集 + 语言模型重打分。

### ✅ 已完成（不用再做）
- 页码：已改成 **`lines[].role` 打标记不删除**（`PRODUCT_SCHEMA` 6），全书 363 行已回填。
- 评测口径：§4 抽查表已统一到剔页码口径；`PAGE_H` 硬编码已去掉（改逐页读 PNG 头）。

---

## 7. 坑点速查（每条都踩过）

| 坑 | 事实 |
|---|---|
| CTC 下的破折号 | `——` 会塌缩成 `—`，这是结构性的 |
| 页码 ≠ 「整行是数字」 | 正文区有 4 行纯数字不是页码（`page_336` 的 `99` 其实是 `——”` 误识） |
| **不要删页码行** | 删了漏行审计会把页码框误报成「有框无行」；只打 `role` 标记 |
| 对齐别用中段探针反推 | 反推式隐含「OCR 偏移 == 参考偏移」，丢字就偏右 → 把本页文字判成「插入」 |
| 对照文本没页码 | 必须靠内容锚定；页 i 终点 = 页 i+1 起点（不重叠划分） |
| `det_boxes` 必须存在 | `[]`（确实没框）与字段缺失是**两种语义**，不能混 |
| `result.boxes` 不能当原始框 | 它与 `txts` 永远 1:1，过滤后的框会让审计永远报 0 差异（假绿） |
| `use_cls: false` 不等于跳过 cls 加载 | `Cls.engine_type` 仍必须指向 openvino，否则 `ImportError` |
| 改 JSON 结构必须 +`PRODUCT_SCHEMA` | 否则旧产物被当可复用，静默混格式 |
| 后台单次 > ~10 分钟被回收 | OCR 必须分批 `--max-minutes 9` |
| pymupdf 版本影响 `pixmap_size()` | 换版本必须重跑拆页回归 |
| Windows `os.replace` | 撞上"目标被占用"抛 `PermissionError`（写侧已有界重试）；写方 replace 瞬间**读方 open 也会被拒** |

---

## 8. 文件地图

```
D:\Project\pdf2epub\
├── .venv\                      # rapidocr 3.9.2 + openvino + PP-OCRv6 medium（620MB，不入库）
├── configs\rapidocr_openvino_ppocrv6_medium.yaml
├── pipeline\
│   ├── split_pages.py          # ① 拆页
│   └── ocr.py                  # ② OCR（含 lines[].role 页码标记）
├── linefix\
│   ├── common.py               # 共用实现层（判据/几何/IO/指纹/OutputLock）
│   └── __init__.py
├── scripts\
│   ├── check_rapidocr_env.py       # 环境自检 35 项
│   ├── ocr_accuracy_report.py      # 评测 → docs/ocr-accuracy-围城.md
│   └── backfill_line_role.py       # 一次性迁移：补 lines[].role（schema 5→6）
├── docs\
│   ├── HANDOFF.md                  # 本文档
│   ├── ocr-accuracy-围城.md        # 现行评测报告（以此为准）
│   └── archive\ocr-accuracy-围城-v1.md   # 已封存的历史快照（数字已作废）
├── orign_pdf\
│   ├── 围城.pdf / 围城.txt         # 源书 + 原文对照文本（版次不同！）
│   ├── pages\                     # 368 张 PNG（56MB，不入库）
│   └── textlines\                 # 368 个 OCR 产物 + ocr_manifest.json（不入库）
└── .workbuddy\memory\             # MEMORY.md（长期约定）+ 按日工作日志
```

---

## 9. 产物格式速览（`*.textlines.json`）

```json
{
  "page": "page_099",
  "line_count": 25,
  "provenance": {
    "backend": "openvino", "det_boxes_source": "raw", "audit_capable": true,
    "alignment": "det_index", "product_schema": 6,
    "page_number_lines": 1, "top2_error": null, "drop_selfcheck": "ok", ...
  },
  "lines": [ {"line_id":1,"orig_line_id":1,"added":false,"det_index":0,
              "bbox":[x,y,w,h],"text":"我们自己…","confidence":0.998,
              "top2":[[37,0.907,"已",0.093], ...], "role":"text"} ],
  "det_boxes": [[x,y,w,h], ...],            // 过滤前的原始框，必须存在
  "dropped_lines": [ {"det_index":3,"bbox":[...],"text":"","score":0.0,
                      "reason":"empty_text","top2":[...]} ]
}
```

- `lines[].role`：`"text"` / `"page_number"`。**EPUB 生成侧按 `role != "page_number"` 取正文。**
- `det_index` 是**证据归属的唯一依据**；`det_boxes[k]` ↔ `lines[].det_index == k`。
- `dropped_lines`：`null` = 本次没采集（`--no-alts`）；`[]` = 采集了、确实没有丢弃行。
