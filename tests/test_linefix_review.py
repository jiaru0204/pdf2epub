# -*- coding: utf-8 -*-
"""纠错层回归 —— `linefix.audit` / `linefix.rescue` / `linefix.recheck` / `eval.score_glyph`。

跑法：cd D:/Project/pdf2epub && .venv/Scripts/python.exe tests/test_linefix_review.py

设计口径（与 `test_ocr_review.py` 一致）：**能纯函数验的绝不起引擎**。
只有「④ 闭环」与「⑧ 端到端」两节真的跑 OCR / 重识别，其余全是假数据。

🔴 最关键的几条（任何一条失守都意味着纠错层在**静默**骗人）：
   · `boxes_from_product` 必须把「字段缺失」和「字段是空数组」分成两种语义
     （`None` vs `0 行`）—— 混起来会把"生产者没接上"读成"这页没有框"。
   · 闭环：删掉一行 → audit 报 1 → rescue 补回 → 再 audit 归 0。
     且补出来的行必须**认领**那个 det 框（`det_index`），否则不变式断了。
   · rescue 的 provenance 必须**合并**源产物，`det_boxes_source` 不能丢 ——
     丢了之后下一次 audit 就再也发现不了降级产物。
   · 判定 ≠ 采纳：`judge` 说「改字」不等于 `should_adopt` 说「写盘」。
   · 评测集是**按书锚定**的：异书台账必须 **exit 2**，不能打一张 0/34 的空表。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from linefix import common as C          # noqa: E402
from linefix import audit as A           # noqa: E402
from linefix import recheck as R         # noqa: E402
from linefix import rescue as RS         # noqa: E402
from eval import score_glyph as SG       # noqa: E402

WORK = ROOT / ".tmp" / "linefixtest"
PY = sys.executable


def _find_cjk_font() -> Path:
    for c in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
              "C:/Windows/Fonts/simsun.ttc", "C:/Windows/Fonts/Deng.ttf"):
        p = Path(c)
        if p.is_file():
            return p
    raise SystemExit("✗ 未找到中文字体，无法合成测试页图")


FONT = _find_cjk_font()

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: Any = "") -> None:
    results.append((name, bool(ok), str(detail)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def run_mod(*argv: str) -> tuple[int, str]:
    p = subprocess.run([PY, *argv], cwd=str(ROOT), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def read_json(p: Path) -> Any:
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------
def make_pages(d: Path) -> None:
    """合成一页中文页图（够 audit/rescue/recheck 用）。

    第二行刻意含「自己」—— ⑧ 节要把产物里的「己」改错成「已」，
    再让 recheck 从**原图**把它读回来（图是对的、产物是错的）。
    """
    from PIL import Image, ImageDraw, ImageFont
    d.mkdir(parents=True, exist_ok=True)
    W, H = 1240, 1754
    img = Image.new("RGB", (W, H), "white")
    dr = ImageDraw.Draw(img)
    rows = [
        ("第一行的内容用来做审计测试", 48, 200),
        ("他知道自己应该马上离开这里", 48, 320),
        ("第三行写一句完全不同的话", 48, 440),
        ("第四行的文字长度明显更长一些以便排序测试", 48, 560),
        ("第五行结束", 48, 680),
    ]
    for text, size, y in rows:
        dr.text((110, y), text, fill="black", font=ImageFont.truetype(str(FONT), size))
    img.save(d / "page_001.png")


# ---------------------------------------------------------------------------
def audit_pure_checks() -> None:
    section("① audit 纯函数：取框口径 / 降级识别 / 丢弃行证据")
    bx, bad = A.boxes_from_product({"det_boxes": None})
    record("字段为 null → None（生产者没落盘，调用方应响亮跳过）", bx is None and bad == 0)
    bx, bad = A.boxes_from_product({})
    record("字段缺失 → None（同上）", bx is None and bad == 0)
    bx, bad = A.boxes_from_product({"det_boxes": []})
    record("字段是空数组 → 0 个框（该页确实没有框，与缺失不同）",
           bx is not None and len(bx) == 0, f"shape={None if bx is None else bx.shape}")
    bx, bad = A.boxes_from_product({"det_boxes": [[10, 20, 30, 40], ["x", 1, 2, 3]]})
    record("坏条目被计数丢弃，好条目照用", bx is not None and len(bx) == 1 and bad == 1,
           f"{len(bx)} 框 / bad={bad}")
    record("四点框重建正确（左上/右上/右下/左下）",
           bx is not None and bx[0].tolist() == [[10, 20], [40, 20], [40, 60], [10, 60]])

    record("det_boxes_source=filtered → 判为降级",
           A.filtered_det_source({"provenance": {"det_boxes_source": "filtered"}}) is True)
    record("det_boxes_source=raw → 不是降级",
           A.filtered_det_source({"provenance": {"det_boxes_source": "raw"}}) is False)
    record("没有 provenance → 不当作降级（不报假警）",
           A.filtered_det_source({}) is False)

    record("dropped_lines=null → None（本次没采集，语义不同于「没丢」）",
           A.dropped_evidence({"dropped_lines": None}) is None)
    record("dropped_lines=[] → []（采集了，确实没丢）",
           A.dropped_evidence({"dropped_lines": []}) == [])
    record("dropped_lines 带 det_index → 取出来",
           A.dropped_evidence({"dropped_lines": [{"det_index": 3}, {"det_index": 7}]}) == [3, 7])


def audit_diff_checks() -> None:
    section("② audit 判定：det − lines 集合差")
    import numpy as np

    def quad(x, y, w, h):
        return np.array([[[x, y], [x + w, y], [x + w, y + h], [x, y + h]]], dtype=float)

    det = np.vstack([quad(100, 100, 400, 40), quad(100, 200, 400, 40), quad(100, 300, 400, 40)])
    prod = [(100.0, 100.0, 400.0, 40.0), (100.0, 200.0, 400.0, 40.0), (100.0, 300.0, 400.0, 40.0)]
    d = A.diff_boxes(det, prod, 30, 14, 0.30, 8)
    record("完全对上 → 0 差异", d == [], d)
    d = A.diff_boxes(det, prod[:2], 30, 14, 0.30, 8)
    record("少一行 → 报 1 条，且 det_index 指对", len(d) == 1 and d[0]["det_index"] == 2, d)
    # 中心包含：det 框比产物行大一圈，中心落在行内 → 视为同一个
    d = A.diff_boxes(np.array([[[90, 90], [520, 90], [520, 150], [90, 150]]], dtype=float),
                     prod[:1], 30, 14, 0.99, 40)
    record("中心包含判定生效（IoU 不到阈值也算匹配）", d == [], d)
    # 太小的框按碎片跳过（不报「整行漏」）
    d = A.diff_boxes(np.array([[[100, 500], [122, 500], [122, 520], [100, 520]]], dtype=float),
                     [], 30, 14, 0.30, 8)
    record("小于最小尺寸的框 → 不报（噪声/标点碎片）", d == [], d)


def rescue_pure_checks() -> None:
    section("③ rescue 的目标收集：没有 audit 结果时不许炸")
    empty = WORK / "empty_products"
    empty.mkdir(parents=True, exist_ok=True)
    P = {"audit_summary": WORK / "nonexistent" / "summary.json",
         "products": empty, "pages": empty}
    args = type("A", (), {"pages_filter": None, "conf_w2": 0.75, "fig_ink": C.FIG_INK})()
    tg, n_fig = RS.collect_targets(args, P, {})
    record("没有 audit summary 时 W1 为空、不抛异常", tg == {} and n_fig == 0, f"{tg} / {n_fig}")


def recheck_judge_checks() -> None:
    section("④ recheck 判定规则：结构式，不靠 conf")
    old = "他知道自己应该离开"

    def tgt(ci=4, kind="cjk", text=old):
        return {"text": text, "char_index": ci, "kind": kind}

    r = R.judge(tgt(), {"text": old[:4] + "已" + old[5:]}, None, 0.25)
    record("长度不变 + 只有目标位不同 → 改字",
           r["verdict"] == "改字" and r["new"] == "已" and r["diff"] == [4], r)
    r = R.judge(tgt(), {"text": old}, None, 0.25)
    record("长度不变 + 一字未动 → 两路一致·不改", r["verdict"] == "两路一致·不改", r["verdict"])
    r = R.judge(tgt(kind="blank"), {"text": old}, 0.10, 0.25)
    record("两路一致但该位几乎没墨 → 疑似无字·待核", r["verdict"] == "疑似无字·待核", r)
    r = R.judge(tgt(), {"text": old[:4] + old[5:]}, None, 0.25)
    record("长度 n−1 且缺的正是目标位 → 删字", r["verdict"] == "删字", r)
    r = R.judge(tgt(), {"text": old[:2] + old[3:]}, None, 0.25)
    record("长度 n−1 但前面就不一致 → 证据不足·待核",
           r["verdict"] == "证据不足·待核", r["verdict"])
    r = R.judge(tgt(), {"text": old[:3] + "甲乙" + old[5:]}, None, 0.25)
    record("长度变了 2 个字 → 对齐失效·证据不足", r["verdict"] == "证据不足·待核", r["verdict"])
    r = R.judge(tgt(), {"text": old[:4] + "已" + old[5:7] + "乙" + old[8:]}, None, 0.25)
    record("同行多处差异 → 非定向·证据不足", r["verdict"] == "证据不足·待核", r["verdict"])
    r = R.judge(tgt(), {"text": ""}, None, 0.25)
    record("二次后验为空 → 证据不足", r["verdict"] == "证据不足·待核" and r["new"] is None, r)


def adopt_policy_checks() -> None:
    section("⑤ 采纳策略：提升小的不用 / 对原文内容有损害的不用")
    record("改字 + cjk → 采纳（双路证据一致）", R.should_adopt("改字", "cjk", "cjk")[0] is True)
    record("改字 + blank（标点/空白类）→ 不采纳",
           R.should_adopt("改字", "blank", "cjk")[0] is False)
    record("删字 → 默认不采纳", R.should_adopt("删字", "cjk", "cjk")[0] is False)
    record("删字 + --adopt all → 才采纳", R.should_adopt("删字", "cjk", "all")[0] is True)
    record("疑似无字·待核 → 不采纳", R.should_adopt("疑似无字·待核", "blank", "cjk")[0] is False)
    record("两路一致·不改 → 不采纳", R.should_adopt("两路一致·不改", "cjk", "cjk")[0] is False)
    record("每条都给出理由（可追溯）",
           all(R.should_adopt(v, k, "cjk")[1] for v in
               ("改字", "删字", "疑似无字·待核", "两路一致·不改") for k in ("cjk", "blank")))


def apply_corrections_checks() -> None:
    section("⑥ 落盘：只动目标位、逐字留痕、schema 跟上")
    base = WORK / "apply"
    if base.exists():
        shutil.rmtree(base)
    products, out = base / "products", base / "corrected"
    lines = [
        {"line_id": 1, "orig_line_id": 1, "added": False, "bbox": [10, 10, 300, 40],
         "text": "他知道自己应该离开", "confidence": 0.99, "det_index": 0, "role": "text"},
        {"line_id": 2, "orig_line_id": 2, "added": False, "bbox": [10, 60, 300, 40],
         "text": "第二行保持原样", "confidence": 0.99, "det_index": 1, "role": "text"},
        {"line_id": 3, "orig_line_id": 3, "added": False, "bbox": [10, 900, 60, 40],
         "text": "7", "confidence": 0.99, "det_index": 2, "role": "page_number"},
    ]
    C.write_lines(products / "page_001.textlines.json", "page_001", lines,
                  {"product_schema": C.PRODUCT_SCHEMA, "det_boxes_source": "raw"},
                  det_boxes=[[10, 10, 300, 40], [10, 60, 300, 40], [10, 900, 60, 40]])

    items = [
        {"page": "page_001", "line_id": 1, "char_index": 4, "old": "己", "new": "已",
         "verdict": "改字", "adopt": True},
        # 产物已经变了（该位不是「己」）→ 必须放弃这一条，不许错位改字
        {"page": "page_001", "line_id": 2, "char_index": 0, "old": "他", "new": "她",
         "verdict": "改字", "adopt": True},
    ]
    n = R.apply_corrections(products, out, items)
    doc = read_json(out / "page_001.textlines.json")
    txt = doc["lines"][0]["text"]
    record("写出 1 页修正产物", n == 1, f"n={n}")
    record("目标位被改对：己 → 已", txt == "他知道自已应该离开", txt)
    record("只动目标位（其余字符逐字不变）",
           txt[:4] == "他知道自" and txt[5:] == "应该离开" and len(txt) == 9, txt)
    record("逐字留痕 text_corrections 落盘",
           doc["lines"][0].get("text_corrections", [{}])[0].get("to") == "已",
           doc["lines"][0].get("text_corrections"))
    record("provenance.product_schema 跟上 PRODUCT_SCHEMA",
           doc["provenance"].get("product_schema") == C.PRODUCT_SCHEMA,
           doc["provenance"].get("product_schema"))
    record("未命中的条目被放弃（防错位）",
           doc["lines"][1]["text"] == "第二行保持原样", doc["lines"][1]["text"])
    record("role 原样保留（页码行不是正文）",
           [l.get("role") for l in doc["lines"]] == ["text", "text", "page_number"],
           [l.get("role") for l in doc["lines"]])


def score_glyph_checks() -> None:
    section("⑦ 裁判（score_glyph）：定位 / 计分 / 采集下界 / 异书台账")
    # 口径照台账来：`token` 是 **OCR 产出的那串**（含错字），`wrong` 是产物里的错字。
    doc = {"lines": [
        {"line_id": 1, "text": "他知道自已应该离开", "bbox": [0, 0, 300, 40]},
        {"line_id": 2, "text": "第二个自已又出现了", "bbox": [0, 50, 300, 40]},
    ]}
    rec = {"id": "x1", "page": "page_001", "wrong": "已", "right": "己", "char_index": 4,
           "group": "已→己", "token": "自已", "occ": 1, "off": 1,
           "ref_line_text": "他知道自已应该离开"}
    loc = SG.locate(rec, doc)
    record("按 token+occ+off 定位到目标位", loc["ok"] and loc["char_index"] == 4, loc)
    bad_ref = dict(rec, ref_line_text="这一行根本不存在")
    record("ref_line_text 对不上 → 拒绝定位（不去猜别的行）",
           SG.locate(bad_ref, doc)["ok"] is False)
    miss = dict(rec, token="不存在的串")
    record("token 找不到 → 拒绝并给出原因", SG.locate(miss, doc)["ok"] is False)

    changes = [("page_001", 0, 4, "己"),      # 命中正样本
               ("page_001", 1, 2, "己")]      # 台账外 → 待复核
    sc = SG.score(changes, [rec], {"page_001": doc})
    record("计分：改对 1 / 待复核 1 / 净收益 0",
           sc["fixed"] == 1 and sc["fp"] == 1 and sc["net"] == 0, sc)

    docs = {"page_001": {"provenance": {"top2_floor": 0.001},
                         "lines": [{"text": "自已", "top2": [[1, 3, 0.9, "己", 0.02]]}]}}
    record("采集下界优先信 provenance 的声明值（不是实测最小值）",
           SG.min_observed_ratio(docs) == 0.001, SG.min_observed_ratio(docs))

    # 异书台账：拿《爱尔兰》的 34 条去评合成产物 → 必须 exit 2
    probe = WORK / "sg_probe" / "products"
    C.write_lines(probe / "page_001.textlines.json", "page_001", [doc["lines"][0]],
                  {"product_schema": C.PRODUCT_SCHEMA}, det_boxes=[])
    rc, log = run_mod("eval/score_glyph.py", "--products", str(probe))
    record("异书台账 → exit 2（绝不打一张 0/34 的空表）", rc == 2, f"rc={rc}")
    record("拒绝理由说得清楚（不是「本书没有形近字错误」）",
           ("台账不是这本书的" in log) or ("台账锚的不是这批产物" in log),
           log.strip().splitlines()[-1] if log.strip() else "")


# ---------------------------------------------------------------------------
def closure_checks() -> None:
    section("⑧ audit → rescue → audit 闭环（真实 OCR 产物 + 真实引擎）")
    pages = WORK / "pages"
    products = WORK / "textlines"
    if WORK.exists():
        shutil.rmtree(WORK)
    make_pages(pages)
    rc, log = run_mod("-m", "pipeline.ocr", "--pages", str(pages), "--out", str(products))
    record("OCR 产出 1 页产物", rc == 0 and (products / "page_001.textlines.json").is_file(), f"rc={rc}")
    base = read_json(products / "page_001.textlines.json")
    n0 = len(base["lines"])
    record("至少 5 行", n0 >= 5, f"{n0} 行")

    # ---- 造一个「有框无行」：删掉第 2 行，det 框保留 ----
    broken_dir = WORK / "broken"
    broken_dir.mkdir(parents=True, exist_ok=True)
    broken = json.loads(json.dumps(base))
    victim = broken["lines"].pop(1)
    write_json(broken_dir / "page_001.textlines.json", broken)

    probe = WORK / "linefix"
    rc, log = run_mod("-m", "linefix.audit", "--pages", str(pages),
                      "--products", str(broken_dir), "--out", str(probe))
    summ = read_json(probe / "audit" / "summary.json")
    record("audit exit 0", rc == 0, f"rc={rc}")
    record("audit 报出 1 条差异", summ["totals"]["diffs"] == 1, summ["totals"])
    record("差异的 det_index 正是被删那行",
           summ["diffs"] and summ["diffs"][0]["det_index"] == victim["det_index"],
           [d["det_index"] for d in summ["diffs"]])
    record("交叉核对报出「产物声明无丢弃、audit 却有差异」（两条独立证据对不上）",
           summ["totals"]["dropped_no_evidence_pages"] == ["page_001"],
           summ["totals"]["dropped_no_evidence_pages"])

    rc, log = run_mod("-m", "linefix.rescue", "--pages", str(pages),
                      "--products", str(broken_dir), "--out", str(probe))
    corrected = probe / "rescue" / "corrected" / "page_001.textlines.json"
    record("rescue exit 0 且写出修正产物", rc == 0 and corrected.is_file(), f"rc={rc}")
    fixed = read_json(corrected)
    record(f"行数补回原值（{n0}）", len(fixed["lines"]) == n0,
           f"{len(fixed['lines'])} 行")
    added = [l for l in fixed["lines"] if l.get("added")]
    record("补出的行标了 added=True", len(added) == 1, f"{len(added)} 条")
    record("补出的行**认领**那个 det 框（det_index 不丢）",
           added and added[0].get("det_index") == victim["det_index"],
           added[0].get("det_index") if added else None)
    record("补出的行有 role（schema 6 必备语义字段）",
           added and added[0].get("role") in ("text", "page_number"),
           added[0].get("role") if added else None)
    inv = all(l["bbox"] == fixed["det_boxes"][l["det_index"]]
              for l in fixed["lines"] if l.get("det_index") is not None)
    record("lines[].bbox == det_boxes[det_index] 仍成立（不变式）", inv)
    record("provenance 透传了 det_boxes_source（否则下次 audit 失明）",
           (fixed.get("provenance") or {}).get("det_boxes_source") == "raw",
           (fixed.get("provenance") or {}).get("det_boxes_source"))
    record("provenance 透传了 top2_floor（采集口径不能丢）",
           (fixed.get("provenance") or {}).get("top2_floor") == 0.001,
           (fixed.get("provenance") or {}).get("top2_floor"))
    record("rescue 留痕写进了 corrections.json",
           (probe / "rescue" / "corrections.json").is_file())

    # ---- 再审计：差异必须归 0 ----
    rc, log = run_mod("-m", "linefix.audit", "--pages", str(pages),
                      "--products", str(probe / "rescue" / "corrected"),
                      "--out", str(probe / "audit2"))
    summ2 = read_json(probe / "audit2" / "audit" / "summary.json")
    record("闭环：修补后再 audit 差异归 0", summ2["totals"]["diffs"] == 0, summ2["totals"])
    record("闭环：没有页面因缺 det_boxes 被跳过",
           summ2["totals"]["no_det_boxes"] == 0, summ2["totals"])


def recheck_e2e_checks() -> None:
    section("⑨ recheck 端到端：候选从产物现取 + 原图二次后验 + 报告")
    pages, products = WORK / "pages", WORK / "textlines"
    base = read_json(products / "page_001.textlines.json")

    # 把产物里「自己」的「己」改错成「已」，并补一条 top2 候选（c1=已 / c2=己）。
    # 原图上是「自己」—— 所以二次后验**有据可依**，方向由原图定，不由人预设。
    corrupted = json.loads(json.dumps(base))
    hit = None
    for ln in corrupted["lines"]:
        t = ln.get("text") or ""
        if "自己" in t:
            ci = t.find("己")
            ln["text"] = t[:ci] + "已" + t[ci + 1:]
            ln.setdefault("top2", []).append([ci, 100, 0.93, "己", 0.05])
            hit = (ln["line_id"], ci, t)
            break
    record("合成页上找到了「自己」（供改造）", hit is not None, hit)
    bad_dir = WORK / "corrupt"
    bad_dir.mkdir(parents=True, exist_ok=True)
    write_json(bad_dir / "page_001.textlines.json", corrupted)

    out = WORK / "recheck"
    rc, log = run_mod("-m", "linefix.recheck", "--pages", str(pages),
                      "--products", str(bad_dir), "--out", str(out))
    record("recheck exit 0", rc == 0, f"rc={rc}")
    rep = read_json(out / "recheck" / "report.json")
    record("报告里记了候选位与判定汇总",
           rep["targets"] >= 1 and isinstance(rep.get("verdicts"), dict), rep.get("verdicts"))
    record("采集口径写进报告（墨量来源可追溯）", "ink_src" in rep, rep.get("ink_src"))
    record("产物口径写进报告（product_schema / pad）",
           (rep.get("provenance") or {}).get("product_schema") == C.PRODUCT_SCHEMA,
           (rep.get("provenance") or {}).get("product_schema"))

    mine = [r for r in rep["items"]
            if hit and r["line_id"] == hit[0] and r["char_index"] == hit[1]]
    record("我们造的那个位置被收集为 cjk 候选", bool(mine), mine[:1])
    if mine:
        r0 = mine[0]
        record("判定与采纳策略一致（判定 ≠ 采纳）",
               r0["adopt"] == R.should_adopt(r0["verdict"], r0["kind"], "cjk")[0],
               f"{r0['verdict']} / adopt={r0['adopt']}")
        record("二次后验确实跑到了（conf 有值，不是裁片失败）",
               r0.get("conf") is not None, r0.get("conf"))
        if r0["verdict"] == "改字":
            record("方向由原图定：已 → 己", r0.get("new") == "己", r0.get("new"))

    # --apply：只写被改动的页，且不许动源产物
    before = (bad_dir / "page_001.textlines.json").read_bytes()
    rc, log = run_mod("-m", "linefix.recheck", "--pages", str(pages),
                      "--products", str(bad_dir), "--out", str(out), "--apply")
    record("--apply 后源产物**一字未动**（非破坏性）",
           (bad_dir / "page_001.textlines.json").read_bytes() == before)
    adop = rep.get("adopted", 0)
    if adop:
        corr = out / "recheck" / "corrected" / "page_001.textlines.json"
        record("有采纳项 → 写出 corrected/ 部分集", corr.is_file())
        if corr.is_file():
            d = read_json(corr)
            record("修正产物的 product_schema 已提升",
                   d["provenance"].get("product_schema") == C.PRODUCT_SCHEMA)
    else:
        record("采纳 0 条 → 不写 corrected/（默认策略下确实没有可写项）",
               not (out / "recheck" / "corrected").exists()
               or not list((out / "recheck" / "corrected").glob("*.json")))


def review_sheet_checks() -> None:
    section("⑩ 复核贴图：清单里的 products 优先，不许靠猜目录")
    pages, products = WORK / "pages", WORK / "corrupt"
    base = read_json(products / "page_001.textlines.json")
    ln = next((l for l in base["lines"] if "自已" in (l.get("text") or "")), None)
    record("合成产物里有可复核的行", ln is not None)
    if ln is None:
        return
    ci = ln["text"].find("已")
    spec = {"thr": 0.05, "total": 1, "known": 0, "unknown": 1,
            "products": str(products),
            "items": [{"page": "page_001", "line_id": ln["line_id"], "char_index": ci,
                       "before": "已", "new": "己", "ratio": 0.0538,
                       "group": "已→己", "bbox": ln["bbox"],
                       "context": "他知道【已】", "known_id": None}]}
    review = WORK / "review_book.json"
    write_json(review, spec)
    out_png = WORK / "review_sheet.png"
    # `--loc even`（字数均分）：本节验的是**贴图链路**（清单→裁片→标签→计数），
    # 字级定位口径已由 `test_ocr_review.py` 的字级定位用例覆盖，这里不重复起引擎。
    rc, log = run_mod("eval/review_sheet.py", "--pages", str(pages),
                      "--review", str(review), "--out", str(out_png), "--loc", "even")
    record("review_sheet exit 0", rc == 0, f"rc={rc}")
    record("贴图落盘且非空", out_png.is_file() and out_png.stat().st_size > 0,
           f"{out_png.stat().st_size if out_png.exists() else 0} B")
    record("产物目录取自清单里的 products（不靠猜）",
           str(products) in log, [l for l in log.splitlines() if "行文本取自" in l])
    record("没有「找不到对应页 / 没裁出图」的告警",
           "条在产物目录里找不到" not in log and "条没能裁出图" not in log,
           [l.strip() for l in log.splitlines() if "⚠" in l][:2])


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    audit_pure_checks()
    audit_diff_checks()
    rescue_pure_checks()
    recheck_judge_checks()
    adopt_policy_checks()
    apply_corrections_checks()
    score_glyph_checks()
    closure_checks()
    recheck_e2e_checks()
    review_sheet_checks()

    bad = [n for n, ok, _ in results if not ok]
    print("\n" + "=" * 66)
    print(f"合计 {len(results)} 项，通过 {len(results) - len(bad)}，失败 {len(bad)}")
    for n in bad:
        print(f"  - {n}")
    print("=" * 66)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
