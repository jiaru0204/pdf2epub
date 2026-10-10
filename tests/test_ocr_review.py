# -*- coding: utf-8 -*-
"""pipeline.ocr 回归 —— 逐条覆盖 review 指出的问题。

跑法：cd D:/Project/pdf2epub && .venv/Scripts/python.exe tests/test_ocr_review.py

🔴 最关键的一条：`lines[m].bbox == det_boxes[lines[m].det_index]`。
   产物行的 bbox 来自「过滤后的框」，而 det_boxes 来自「过滤前的框」；两道过滤
   只做删除、不重排也不改坐标，所以两者必须**逐值相等**。只要 det 下标归位有
   任何偏差（argsort 复现错 / 批次映射错位 / 文本反查猜错），这条必然失败。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WORK = ROOT / ".tmp" / "ocrtest"
PY = sys.executable
def _find_cjk_font() -> Path:
    """常见位置找一个中文字体（不能写死单一路径，换台机器就可能没有）。"""
    for c in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
              "C:/Windows/Fonts/simsun.ttc", "C:/Windows/Fonts/Deng.ttf"):
        p = Path(c)
        if p.is_file():
            return p
    raise SystemExit("✗ 未找到中文字体（msyh/simhei/simsun/Deng），无法合成测试页图")


FONT = _find_cjk_font()

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def run(*argv: str) -> tuple[int, str]:
    p = subprocess.run([PY, "-m", "pipeline.ocr", *argv], cwd=str(ROOT),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def product(out: Path, page: str) -> dict:
    return json.loads((out / f"{page}.textlines.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
def make_pages(d: Path) -> None:
    """造几页可控的页图：含重复文本行、两种字号、空白页。"""
    from PIL import Image, ImageDraw, ImageFont
    d.mkdir(parents=True, exist_ok=True)
    W, H = 1240, 1754

    # page_001：含两行**文本完全相同**的行（一行 44px、一行 56px），外加不同宽度的行
    img = Image.new("RGB", (W, H), "white")
    dr = ImageDraw.Draw(img)
    rows = [
        ("重复行测试", 44, 120),
        ("第一行内容各不相同", 44, 220),
        ("重复行测试", 56, 320),          # 同文本、不同字宽 → argsort 会把它排走
        ("第三行的文字长度明显更长一些用于测试排序", 44, 430),
        ("短句", 44, 540),
        ("结尾一行", 44, 640),
    ]
    for text, size, y in rows:
        dr.text((110, y), text, fill="black", font=ImageFont.truetype(str(FONT), size))
    img.save(d / "page_001.png")

    # page_002：普通三行
    img = Image.new("RGB", (W, H), "white")
    dr = ImageDraw.Draw(img)
    for i, text in enumerate(["页二第一行", "页二第二行的内容", "页二第三行"]):
        dr.text((140, 260 + i * 110), text, fill="black",
                font=ImageFont.truetype(str(FONT), 46))
    img.save(d / "page_002.png")

    # page_003：全白 —— 应当 det_boxes == [] 且 lines == []
    Image.new("RGB", (W, H), "white").save(d / "page_003.png")


# --------------------------------------------------------------------------
def structural_checks(out: Path, pages_dir: Path, names: list[str]) -> None:
    section("① 产物结构与 det_index 对齐不变式（核心）")
    total_lines = total_det = 0
    for n in names:
        d = product(out, n)
        prov = d.get("provenance") or {}
        lines = d.get("lines") or []
        det = d.get("det_boxes")
        dropped = d.get("dropped_lines")
        total_lines += len(lines)
        total_det += len(det or [])

        record(f"{n}: det_boxes 字段存在且是列表", isinstance(det, list), str(type(det).__name__))
        record(f"{n}: dropped_lines 是列表（alts 已开）", isinstance(dropped, list))
        record(f"{n}: alignment 合法（有框=det_index，空白页=empty）",
               prov.get("alignment") in ("det_index", "empty"), str(prov.get("alignment")))
        record(f"{n}: alignment 与是否有 det 框一致",
               prov.get("alignment") == ("det_index" if det else "empty"),
               f"alignment={prov.get('alignment')} det={len(det)}")
        record(f"{n}: audit_capable == True", prov.get("audit_capable") is True)
        record(f"{n}: drop_selfcheck == ok", prov.get("drop_selfcheck") == "ok",
               str(prov.get("drop_selfcheck")))
        record(f"{n}: top2_error 为 None", prov.get("top2_error") is None,
               str(prov.get("top2_error")))

        # 🔴 不变式一：产物行的 bbox 必须等于 det_boxes[det_index]
        bad = [m for m, ln in enumerate(lines)
               if not (isinstance(ln.get("det_index"), int)
                       and 0 <= ln["det_index"] < len(det)
                       and list(ln["bbox"]) == list(det[ln["det_index"]]))]
        record(f"{n}: lines[].bbox == det_boxes[det_index]（{len(lines)} 行逐行核对）",
               not bad, f"不符 {len(bad)} 行" if bad else "")

        # 不变式二：det_index 严格递增（顺序 = det 顺序）
        idxs = [ln.get("det_index") for ln in lines]
        record(f"{n}: det_index 严格递增", idxs == sorted(idxs) and len(set(idxs)) == len(idxs),
               str(idxs))

        # 不变式三：kept ∪ dropped 恰好是 range(len(det_boxes)) 的一个划分
        didx = [x.get("det_index") for x in dropped]
        record(f"{n}: lines[].det_index 与 dropped_lines[].det_index 无交集",
               not (set(idxs) & set(didx)))
        record(f"{n}: 两者并集 == 全部 det 框下标",
               sorted(set(idxs) | set(didx)) == list(range(len(det))),
               f"并集 {len(set(idxs) | set(didx))} vs det {len(det)}")

        # 不变式四：dropped 的 bbox 也必须对上
        dbad = [x for x in dropped
                if x.get("bbox") is not None and list(x["bbox"]) != list(det[x["det_index"]])]
        record(f"{n}: dropped_lines[].bbox == det_boxes[det_index]", not dbad, f"不符 {len(dbad)}")
        record(f"{n}: dropped_lines 的 det_index 递增", didx == sorted(didx), str(didx))

        # top2 自洽
        t2bad = []
        for ln in lines:
            for e in (ln.get("top2") or []):
                if not (isinstance(e, list) and len(e) == 5 and 0 <= e[0] < len(ln["text"])):
                    t2bad.append((ln.get("det_index"), e))
        record(f"{n}: top2 条目 i 落在行内字符范围内", not t2bad, str(t2bad[:2]))
        record(f"{n}: reason 取值合法",
               all(x.get("reason") in ("empty_text", "low_score", "unresolved") for x in dropped),
               str(sorted({x.get("reason") for x in dropped})))

    print(f"  （本批合计 {total_lines} 行 / {total_det} 个 det 框）")


def duplicate_text_check(out: Path, pages_dir: Path) -> None:
    section("② 重复文本行：两条同文本的行必须各自归属自己的 det 框")
    d = product(out, "page_001")
    lines = d.get("lines") or []
    dups = [ln for ln in lines if ln["text"] == "重复行测试"]
    record("page_001 识别出两条「重复行测试」", len(dups) == 2, f"{len(dups)} 条")
    if len(dups) == 2:
        record("两条的 det_index 不同", dups[0]["det_index"] != dups[1]["det_index"],
               str([x["det_index"] for x in dups]))
        det = d["det_boxes"]
        record("两条各自的 bbox 与自己的 det 框一致",
               all(list(x["bbox"]) == list(det[x["det_index"]]) for x in dups))
        # 两条字号不同（44 vs 56）→ 框高必然不同；若被文本反查互换，这条会失败
        record("两条 bbox 高度不同（字号不同，确认是两行不同的像素）",
               dups[0]["bbox"][3] != dups[1]["bbox"][3],
               f"{dups[0]['bbox']} vs {dups[1]['bbox']}")

    # 干净合成图上绝大多数位置的 p2/p1 远小于 1e-3，默认下限会把它们全滤掉 ——
    # 这本身是**预期行为**，不是缺陷。要验证"每个字都有证据且归属正确"，把采集下限
    # 临时调到 0 独立跑一遍，断言每行 top2 条目数与 text 等长。
    import pipeline.ocr as O
    real = O.TOP2_FLOOR
    o = WORK / "floor0"
    if o.exists():
        shutil.rmtree(o)
    rc = -1
    try:
        O.TOP2_FLOOR = 0.0                 # 0 = 每个解码字符都存
        rc = O.main(["--pages", str(pages_dir), "--out", str(o)])
    finally:
        O.TOP2_FLOOR = real
    record("采集下限=0 的独立跑 exit 0", rc == 0, f"rc={rc}")
    d0 = product(o, "page_001")
    bad0 = [ln["det_index"] for ln in d0["lines"]
            if len(ln.get("top2") or []) != len(ln["text"])]
    record("下限=0 时每行 top2 条目数 == 字符数（每个字都有证据）", not bad0, f"不符 {bad0}")
    det0 = d0["det_boxes"]
    b0 = [m for m, ln in enumerate(d0["lines"])
          if list(ln["bbox"]) != list(det0[ln["det_index"]])]
    record("下限=0 时 det_index 对齐不变式仍成立", not b0, f"不符 {len(b0)} 行")
    dup0 = [ln for ln in d0["lines"] if ln["text"] == "重复行测试"]
    record("下限=0 时两条同文本行各有等长 top2（未互相串位）",
           len(dup0) == 2 and all(len(x["top2"]) == len(x["text"]) for x in dup0),
           str([len(x.get("top2") or []) for x in dup0]))
    record("下限=0 时 provenance.top2_floor == 0.0（口径变化被如实记录）",
           d0["provenance"].get("top2_floor") == 0.0, str(d0["provenance"].get("top2_floor")))


def blank_page_check(out: Path) -> None:
    section("③ 空白页：det_boxes=[] 与字段缺失必须区分")
    d = product(out, "page_003")
    record("det_boxes 是空列表（不是缺失）", d.get("det_boxes") == [])
    record("lines 为空", (d.get("lines") or []) == [])
    record("provenance 仍完整（alignments/audit_capable 在）",
           d["provenance"].get("audit_capable") is True)


def reuse_checks(pages_dir: Path, out: Path) -> None:
    section("④ 断点续跑与过期守卫")
    rc, log = run("--pages", str(pages_dir), "--out", str(out))
    record("重跑 exit 0", rc == 0, f"rc={rc}")
    record("全部可复用（无需处理）", "都可复用，无需处理" in log,
           [l for l in log.splitlines() if "可复用" in l][-1:] or log.strip().splitlines()[-1:])
    mtimes = {p.name: p.stat().st_mtime_ns for p in out.glob("*.textlines.json")}
    rc, log = run("--pages", str(pages_dir), "--out", str(out))
    record("再跑一次也没改动产物",
           mtimes == {p.name: p.stat().st_mtime_ns for p in out.glob("*.textlines.json")})

    # 换页图内容 → 必须判过期
    from PIL import Image, ImageDraw, ImageFont
    img = Image.open(pages_dir / "page_002.png")
    dr = ImageDraw.Draw(img)
    dr.text((140, 900), "新加的一行让图变了", fill="black",
            font=ImageFont.truetype(str(FONT), 46))
    img.save(pages_dir / "page_002.png")
    rc, log = run("--pages", str(pages_dir), "--out", str(out))
    record("页图变了 → 该页被判不可复用并重跑", "产物已过期" in log or "重跑" in log)
    d2 = product(out, "page_002")
    record("重跑后新页图的行数增加（说明确实重跑了）", len(d2["lines"]) >= 4,
           f"{len(d2['lines'])} 行")
    # 恢复
    make_pages(pages_dir)


def fingerprint_checks(cfg: Path) -> None:
    section("⑤ 口径指纹：--backend 必须进指纹")
    from linefix import common as C
    a = C.ocr_fingerprint(cfg, alts=True, top2_floor=1e-3, top2_decimals=4,
                          backend="auto", det_boxes_source="raw", hook_schema=1)
    b = C.ocr_fingerprint(cfg, alts=True, top2_floor=1e-3, top2_decimals=4,
                          backend="onnxruntime", det_boxes_source="raw", hook_schema=1)
    record("backend override 被记录", a.get("backend_override") == "auto"
           and b.get("backend_override") == "onnxruntime")
    record("生效后端随之改变", a.get("backend") == "openvino" and b.get("backend") == "onnxruntime",
           f"{a.get('backend')} / {b.get('backend')}")
    record("两者指纹确实不同", C.fingerprint_drift(a, b) != [], str(C.fingerprint_drift(a, b)[:2]))
    # alts / det_boxes_source / schema 都进指纹
    c = C.ocr_fingerprint(cfg, alts=False, top2_floor=1e-3, top2_decimals=4,
                          backend="auto", det_boxes_source="filtered", hook_schema=1)
    diff_keys = {x.split(":")[0] for x in C.fingerprint_drift(a, c)}
    record("alts / det_boxes_source 都在指纹里",
           {"alt_capture", "det_boxes_source"} <= diff_keys, str(sorted(diff_keys)))
    record("指纹含运行时版本与权重哈希",
           all(k in a for k in ("rapidocr", "openvino", "numpy", "model_sha256")),
           str(sorted(k for k in a if k in ("rapidocr", "openvino", "numpy"))))
    record("权重哈希是真实文件哈希（不是 '?'）",
           all(v not in ("?", None) for v in (a.get("model_sha256") or {}).values()),
           str(a.get("model_sha256")))
    record("product_schema == 6（加了 det_index 与 role 字段）",
           a.get("product_schema") == 6)


def cli_checks(pages_dir: Path, out: Path, cfg: Path) -> None:
    section("⑥ CLI 严格化")
    for argv, why in [
        (["--pages", str(pages_dir), "--out", str(out), "--limit", "-1"], "--limit 负数"),
        (["--pages", str(pages_dir), "--out", str(out), "--max-minutes", "-5"], "--max-minutes 负数"),
        (["--pages", str(pages_dir), "--out", str(out), "--backend", "tensorrt"], "--backend 非法"),
        (["--pages", str(pages_dir), "--out", str(out), "--pages-filter", "page_999"], "过滤后无页"),
    ]:
        rc, log = run(*argv)
        record(f"{why} → exit 2 且无 traceback", rc == 2 and "Traceback" not in log, f"rc={rc}")

    bad = WORK / "broken.yaml"
    bad.write_text("Global: [oops\n", encoding="utf-8")
    rc, log = run("--pages", str(pages_dir), "--out", str(out), "--config", str(bad))
    record("配置解析失败 → exit 2（不是降级警告）", rc == 2, f"rc={rc}")
    record("错误提到指纹不完整", "口径指纹不完整" in log, log.strip().splitlines()[0] if log else "")

    rc, log = run("--pages", str(pages_dir), "--out", str(out), "--pages-filter", "abc")
    record("--pages-filter 无法归一 → 非零退出", rc != 0, f"rc={rc}")


def no_alts_check(pages_dir: Path) -> None:
    section("⑦ --no-alts：显式降级要留下痕迹")
    o = WORK / "noalts"
    if o.exists():
        shutil.rmtree(o)
    rc, log = run("--pages", str(pages_dir), "--out", str(o), "--no-alts", "--limit", "1")
    record("exit 0", rc == 0, f"rc={rc}")
    d = product(o, "page_001")
    record("dropped_lines 为 null（= 没采集，不是「没有」）",
           d.get("dropped_lines") is None, repr(d.get("dropped_lines")))
    prov = d["provenance"]
    record("provenance.alt_capture == False", prov.get("alt_capture") is False)
    record("provenance.top2_floor 为 null", prov.get("top2_floor") is None)
    record("lines 里没有 top2 字段", all("top2" not in ln for ln in d["lines"]))
    record("stdout 明确提示没采证据", "没有**采集 top2" in log or "没有**采集" in log
           or "--no-alts" in log)


def degraded_det_check(pages_dir: Path) -> None:
    section("⑧ 钩子失效：默认拒绝；显式降级要标 audit_capable=False")
    import pipeline.ocr as O
    o = WORK / "filtered"
    if o.exists():
        shutil.rmtree(o)

    real = O.install_raw_det_hook
    try:
        O.install_raw_det_hook = lambda: False
        rc = O.main(["--pages", str(pages_dir), "--out", str(o), "--limit", "1"])
        record("raw-det 钩子失效 + 未加 --allow-filtered-det → exit 1", rc == 1, f"rc={rc}")

        rc = O.main(["--pages", str(pages_dir), "--out", str(o), "--limit", "1",
                     "--allow-filtered-det"])
        record("加 --allow-filtered-det → 能跑（exit 0）", rc == 0, f"rc={rc}")
        d = product(o, "page_001")
        prov = d["provenance"]
        record("provenance.det_boxes_source == filtered", prov.get("det_boxes_source") == "filtered")
        record("provenance.audit_capable == False（下游据此拒绝审计）",
               prov.get("audit_capable") is False)
        record("产物里 det_boxes 仍存在（是过滤后的框）", isinstance(d.get("det_boxes"), list))
        record("dropped_lines[].bbox 为 null（过滤后拿不到原始框）",
               all(x.get("bbox") is None for x in (d.get("dropped_lines") or [])))
    finally:
        O.install_raw_det_hook = real


def version_gate_check(pages_dir: Path) -> None:
    section("⑨ 版本闸门：未验证的 rapidocr 版本拒绝生产")
    import pipeline.ocr as O
    real = O.md.version
    try:
        O.md.version = lambda name: "3.10.0" if name == "rapidocr" else real(name)
        rc = O.main(["--pages", str(pages_dir), "--out", str(WORK / "ver")])
        record("未验证版本 → exit 2", rc == 2, f"rc={rc}")
    finally:
        O.md.version = real


def lock_check(pages_dir: Path) -> None:
    section("⑩ 并发：输出目录锁")
    o = WORK / "lock"
    o.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [PY, "-c",
         "import sys,time; sys.path.insert(0, r'%s');" % ROOT +
         "from linefix.common import OutputLock; from pathlib import Path;"
         "lk = OutputLock(Path(r'%s') / '.ocr.lock');" % o +
         "assert lk.acquire(); print('LOCKED', flush=True); time.sleep(12)"],
        cwd=str(ROOT), stdout=subprocess.PIPE, text=True)
    holder.stdout.readline()
    try:
        rc, log = run("--pages", str(pages_dir), "--out", str(o), "--limit", "1")
    finally:
        holder.kill()
        holder.wait(timeout=10)
    record("第二个实例 exit 2", rc == 2, f"rc={rc}")
    record("提示被占用", "另一个 pipeline.ocr" in log, log.strip().splitlines()[0] if log else "")


def dry_run_check(pages_dir: Path, out: Path) -> None:
    section("⑪ dry-run 与正式运行判定一致")
    rc, log = run("--pages", str(pages_dir), "--out", str(out), "--dry-run")
    record("dry-run exit 0", rc == 0, f"rc={rc}")
    record("dry-run 说全部可复用", "可复用，跳过" in log)
    o2 = WORK / "dry2"
    if o2.exists():
        shutil.rmtree(o2)
    rc, log = run("--pages", str(pages_dir), "--out", str(o2), "--dry-run")
    record("空目录 dry-run 说全部需新跑", "需新跑" in log)
    mtimes = list(o2.glob("*")) if o2.exists() else []
    record("dry-run 不写盘", not mtimes, str(mtimes))


# --------------------------------------------------------------------------
def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    pages_dir = WORK / "pages"
    out = WORK / "textlines"
    make_pages(pages_dir)
    cfg = ROOT / "configs" / "rapidocr_openvino_ppocrv6_medium.yaml"

    section("⓪ 首次 OCR")
    rc, log = run("--pages", str(pages_dir), "--out", str(out))
    record("exit 0", rc == 0, f"rc={rc}")
    names = sorted(p.stem for p in pages_dir.glob("*.png"))
    record("三页都产出产物", all((out / f"{n}.textlines.json").is_file() for n in names), str(names))
    record("page_001 至少识别出 6 行", len(product(out, "page_001")["lines"]) >= 6,
           f"{len(product(out, 'page_001')['lines'])} 行")

    structural_checks(out, pages_dir, names)
    duplicate_text_check(out, pages_dir)
    blank_page_check(out)
    reuse_checks(pages_dir, out)
    fingerprint_checks(cfg)
    cli_checks(pages_dir, out, cfg)
    no_alts_check(pages_dir)
    degraded_det_check(pages_dir)
    version_gate_check(pages_dir)
    lock_check(pages_dir)
    dry_run_check(pages_dir, out)

    bad = [n for n, ok, _ in results if not ok]
    print("\n" + "=" * 66)
    print(f"合计 {len(results)} 项，通过 {len(results) - len(bad)}，失败 {len(bad)}")
    for n in bad:
        print(f"  - {n}")
    print("=" * 66)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
