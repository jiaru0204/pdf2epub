# -*- coding: utf-8 -*-
"""split_pages 回归矩阵 —— 逐条覆盖 review 里给出的测试矩阵。

跑法：cd D:/Project/pdf2epub && .venv/Scripts/python.exe tests/test_split_pages_matrix.py
（.tmp/ 在 .gitignore 里，属一次性验证脚本）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WORK = ROOT / ".tmp" / "matrix"
PY = sys.executable

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def run(*argv: str, cwd: Path = ROOT) -> tuple[int, str]:
    p = subprocess.run([PY, "-m", "pipeline.split_pages", *argv],
                       cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def make_pdf(path: Path, pages: list[tuple[float, float]]) -> None:
    import pymupdf
    doc = pymupdf.open()
    for w, h in pages:
        doc.new_page(width=w, height=h)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()


def manifest(d: Path) -> dict:
    return json.loads((d / "split_manifest.json").read_text(encoding="utf-8"))


def pngs(d: Path) -> list[str]:
    return sorted(p.name for p in d.glob("*.png"))


def fake_png(w: int, h: int) -> bytes:
    """只要前 24 字节合法（magic + IHDR 长度 13 + W/H），够 png_size 判读。"""
    return b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR" + \
        w.to_bytes(4, "big") + h.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00"


# --------------------------------------------------------------------------
def main() -> int:
    import pymupdf
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    A = WORK / "book_a.pdf"
    B = WORK / "book_b.pdf"
    SIZES_A = [(595, 842)] * 10 + [(842, 595), (500, 700)]
    make_pdf(A, SIZES_A)
    make_pdf(B, [(595, 842)] * 4)
    print(f"测试 PDF：A={len(SIZES_A)} 页  B=4 页")

    # ---------------- S1/S2/S3 基本续跑 ----------------
    section("S1 正常首次渲染")
    d = WORK / "s1"
    rc, out = run("--pdf", str(A), "--out", str(d))
    m = manifest(d)
    record("首次渲染 exit 0", rc == 0, f"rc={rc}")
    record("产出 12 张 PNG", len(pngs(d)) == 12, f"{len(pngs(d))}")
    record("manifest.rendered == 12", m["rendered"] == 12, f"{m['rendered']}")
    record("manifest 记录身份字段齐全", all(k in m for k in
           ("pdf_sha256", "dpi", "renderer", "prefix", "digits")), str(sorted(m)))

    section("S2 同 PDF 同 DPI 再跑 → 全部 skip")
    before = {p: (d / p).stat().st_mtime_ns for p in pngs(d)}
    rc, out = run("--pdf", str(A), "--out", str(d))
    after = {p: (d / p).stat().st_mtime_ns for p in pngs(d)}
    record("exit 0", rc == 0, f"rc={rc}")
    record("本次渲染 0 页", "渲染 0 页" in out, out.strip().splitlines()[-1] if out else "")
    record("无文件被改动", before == after)
    record("manifest 仍 12 页", manifest(d)["rendered"] == 12)

    section("S3 同 PDF --force → 全部重渲")
    rc, out = run("--pdf", str(A), "--out", str(d), "--force")
    record("exit 0", rc == 0, f"rc={rc}")
    record("本次渲染 12 页", "渲染 12 页" in out)
    record("manifest 仍 12 页", manifest(d)["rendered"] == 12)

    # ---------------- S4/S5 配置漂移必须拒绝 ----------------
    section("S4 同 PDF 改 DPI → 拒绝")
    snap = manifest(d)
    rc, out = run("--pdf", str(A), "--out", str(d), "--dpi", "150")
    record("exit 2", rc == 2, f"rc={rc}")
    record("错误提到 dpi", "dpi" in out, out.strip().splitlines()[1] if out else "")
    record("manifest 未被改写（仍 300）", manifest(d)["dpi"] == 300, f"{manifest(d)['dpi']}")
    record("manifest 内容与拒绝前一致", manifest(d)["pdf_sha256"] == snap["pdf_sha256"])
    record("没有多出 PNG", len(pngs(d)) == 12, f"{len(pngs(d))}")

    section("S5 同 PDF 改 prefix → 拒绝")
    rc, out = run("--pdf", str(A), "--out", str(d), "--prefix", "fig")
    record("exit 2", rc == 2, f"rc={rc}")
    record("错误提到 prefix", "prefix" in out)

    section("S6 不同 PDF 同 out → 拒绝")
    rc, out = run("--pdf", str(B), "--out", str(d))
    record("exit 2", rc == 2, f"rc={rc}")
    record("错误提到拒绝续跑", "拒绝续跑" in out)
    record("manifest 仍是 A 的", manifest(d)["pdf_sha256"] == snap["pdf_sha256"])

    section("S7 不同 PDF + --force → 仍然拒绝")
    rc, out = run("--pdf", str(B), "--out", str(d), "--force", "--pages", "1-10")
    record("exit 2", rc == 2, f"rc={rc}")
    record("--force 不再能绕过来源保护", "拒绝续跑" in out)
    record("PNG 数量未变（没有被混进 B 的页）", len(pngs(d)) == 12, f"{len(pngs(d))}")

    # ---------------- S8/S9 来源存疑必须拒绝 ----------------
    section("S8 manifest 损坏 → 拒绝（fail closed）")
    d8 = WORK / "s8"
    shutil.copytree(d, d8)
    (d8 / "split_manifest.json").write_text('{"pdf_sha256": "half', encoding="utf-8")
    rc, out = run("--pdf", str(A), "--out", str(d8))
    record("exit 2", rc == 2, f"rc={rc}")
    record("错误说明无法解析 manifest", "无法解析" in out)
    record("PNG 保持原样未被跳过式污染", len(pngs(d8)) == 12)

    section("S9 manifest 不存在但 PNG 存在 → 拒绝（不得盲 skip）")
    d9 = WORK / "s9"
    shutil.copytree(d, d9)
    (d9 / "split_manifest.json").unlink()
    rc, out = run("--pdf", str(A), "--out", str(d9))
    record("exit 2", rc == 2, f"rc={rc}")
    record("错误指出缺少 manifest", "但没有 split_manifest.json" in out, out.strip().splitlines()[0] if out else "")
    record("没有生成只含 0 页的新 manifest", not (d9 / "split_manifest.json").exists())

    # ---------------- S10/S11/S12 弱校验必须重渲 ----------------
    section("S10 0-byte PNG → 重渲")
    d10 = WORK / "s10"
    shutil.copytree(d, d10)
    (d10 / "page_003.png").write_bytes(b"")
    rc, out = run("--pdf", str(A), "--out", str(d10))
    record("exit 0", rc == 0, f"rc={rc}")
    record("page_003 被重渲（非 0 字节）", (d10 / "page_003.png").stat().st_size > 1000)
    record("本次渲染 1 页", "渲染 1 页" in out)
    record("manifest 仍 12 页", manifest(d10)["rendered"] == 12)

    section("S11 非空但损坏的 PNG → 重渲")
    d11 = WORK / "s11"
    shutil.copytree(d, d11)
    (d11 / "page_004.png").write_bytes(b"this is not a png at all, but it is non-empty")
    rc, out = run("--pdf", str(A), "--out", str(d11))
    record("exit 0", rc == 0, f"rc={rc}")
    record("本次渲染 1 页", "渲染 1 页" in out)
    record("page_004 恢复为合法 PNG", (d11 / "page_004.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n")

    section("S12 PNG 尺寸与记录不符 → 重渲")
    d12 = WORK / "s12"
    shutil.copytree(d, d12)
    (d12 / "page_005.png").write_bytes(fake_png(10, 10))
    rc, out = run("--pdf", str(A), "--out", str(d12))
    record("exit 0", rc == 0, f"rc={rc}")
    record("本次渲染 1 页", "渲染 1 页" in out)
    record("page_005 尺寸已修正", (d12 / "page_005.png").stat().st_size > 1000)

    # ---------------- S13/S14/S15 参数校验 ----------------
    section("S13 --pages abc → 友好报错、无 traceback")
    rc, out = run("--pdf", str(A), "--out", str(WORK / "s13"), "--pages", "abc")
    record("exit 2", rc == 2, f"rc={rc}")
    record("无 Traceback", "Traceback" not in out)
    record("提示解析失败", "--pages 解析失败" in out)

    section("S14 --pages 99999 → 非零退出")
    rc, out = run("--pdf", str(A), "--out", str(WORK / "s14"), "--pages", "99999")
    record("exit 2", rc == 2, f"rc={rc}")
    record("提示没有任何有效页", "没有任何有效页" in out)
    record("没有留下输出目录", not (WORK / "s14").exists())

    section("S14b --pages 1,2,9999 → 部分越界要告警")
    rc, out = run("--pdf", str(A), "--out", str(WORK / "s14b"), "--pages", "1,2,9999")
    record("exit 0", rc == 0, f"rc={rc}")
    record("告警忽略越界页", "忽略越界页码" in out)

    section("S15 --limit -1 → 友好报错")
    rc, out = run("--pdf", str(A), "--out", str(WORK / "s15"), "--limit", "-1")
    record("exit 2", rc == 2, f"rc={rc}")
    record("无 Traceback", "Traceback" not in out)
    record("提示 --limit 必须 >= 0", "--limit 必须 >= 0" in out)

    section("S15b --prefix 越界/非法 → 拒绝")
    for bad in ("../foo", "a/b", "..", ""):
        rc, out = run("--pdf", str(A), "--out", str(WORK / "s15b"), "--prefix", bad)
        record(f"--prefix {bad!r} 被拒", rc == 2 and "prefix 不合法" in out, f"rc={rc}")

    section("S15c --out 指向已存在的文件 → 友好报错")
    fpath = WORK / "afile.txt"
    fpath.write_text("x", encoding="utf-8")
    rc, out = run("--pdf", str(A), "--out", str(fpath))
    record("exit 2", rc == 2, f"rc={rc}")
    record("提示不是目录", "但不是目录" in out)

    # ---------------- S16 渲染失败 ----------------
    section("S16 某页渲染异常 → exit 1，manifest 状态准确")
    import pipeline.split_pages as S
    d16 = WORK / "s16"
    real_render = S.render_page
    try:
        def boom(page, dpi):
            if page.number == 4:      # 第 5 页
                raise RuntimeError("simulated renderer failure")
            return real_render(page, dpi)
        S.render_page = boom
        rc = S.main(["--pdf", str(A), "--out", str(d16)])
    finally:
        S.render_page = real_render
    m16 = manifest(d16)
    names16 = {x["name"] for x in m16["pages"]}
    record("exit 1", rc == 1, f"rc={rc}")
    record("page_005 不在 manifest 里", "page_005" not in names16)
    record("manifest 记录了 11 页", m16["rendered"] == 11, f"{m16['rendered']}")
    record("rendered == 磁盘上被覆盖的 PNG 数", m16["rendered"] == len([n for n in pngs(d16) if n != "page_005.png"]),
           f"{m16['rendered']} vs {len(pngs(d16))}")

    # ---------------- S17 中断续跑 ----------------
    section("S17 中途 Ctrl+C → 能正确恢复")
    d17 = WORK / "s17"
    calls = {"n": 0}
    try:
        def interrupt(page, dpi):
            calls["n"] += 1
            if calls["n"] == 4:
                raise KeyboardInterrupt
            return real_render(page, dpi)
        S.render_page = interrupt
        rc = S.main(["--pdf", str(A), "--out", str(d17)])
    except KeyboardInterrupt:
        rc = "raised"
    finally:
        S.render_page = real_render
    record("中断返回 130", rc == 130, f"rc={rc}")
    record("中断后 manifest 已落盘", (d17 / "split_manifest.json").exists())
    m17 = manifest(d17)
    record("manifest 记录 3 页（已完成的）", m17["rendered"] == 3, f"{m17['rendered']}")
    record("磁盘上有 3 张 PNG", len(pngs(d17)) == 3, f"{len(pngs(d17))}")
    rc, out = run("--pdf", str(A), "--out", str(d17))
    record("续跑 exit 0", rc == 0, f"rc={rc}")
    record("续跑跳过 3 页", "跳过 3" in out, [l for l in out.splitlines() if "DONE" in l])
    record("续跑后 manifest 满 12 页", manifest(d17)["rendered"] == 12, f"{manifest(d17)['rendered']}")
    record("全部 PNG 齐 12", len(pngs(d17)) == 12)

    section("S17b PNG 在、manifest 缺该条（中断残留）→ 复核后补记")
    d17b = WORK / "s17b"
    shutil.copytree(d, d17b)
    mp = d17b / "split_manifest.json"
    mm = json.loads(mp.read_text(encoding="utf-8"))
    mm["pages"] = [x for x in mm["pages"] if x["name"] not in ("page_008", "page_009")]
    mm["rendered"] = len(mm["pages"])
    mp.write_text(json.dumps(mm, ensure_ascii=False, indent=1), encoding="utf-8")
    rc, out = run("--pdf", str(A), "--out", str(d17b))
    record("exit 0", rc == 0, f"rc={rc}")
    record("补记 2 页", "补记 2" in out)
    record("manifest 恢复 12 页", manifest(d17b)["rendered"] == 12, f"{manifest(d17b)['rendered']}")

    # ---------------- S18 并发 ----------------
    section("S18 两个实例同时启动 → 第二个被锁挡住")
    d18 = WORK / "s18"
    d18.mkdir()
    holder = subprocess.Popen(
        [PY, "-c",
         "import sys,time; sys.path.insert(0,r'%s');" % ROOT +
         "from linefix.common import OutputLock;"
         "from pathlib import Path;"
         "lk=OutputLock(Path(r'%s')/'.split_pages.lock');" % d18 +
         "assert lk.acquire(); print('LOCKED', flush=True); time.sleep(12)"],
        cwd=str(ROOT), stdout=subprocess.PIPE, text=True)
    holder.stdout.readline()
    try:
        rc, out = run("--pdf", str(A), "--out", str(d18), "--limit", "1")
    finally:
        holder.kill()
        holder.wait(timeout=10)
    record("第二个实例 exit 2", rc == 2, f"rc={rc}")
    record("提示被占用 / 并发危害", "另一个 split_pages" in out, out.strip().splitlines()[0] if out else "")

    # ---------------- S19 页数 > 999 命名宽度 ----------------
    section("S19 页数 > 999 → 命名宽度自动加宽")
    big = WORK / "big.pdf"
    bdoc = pymupdf.open()
    for _ in range(1005):
        bdoc.new_page(width=595, height=842)
    bdoc.save(str(big))
    bdoc.close()
    d19 = WORK / "s19"
    rc, out = run("--pdf", str(big), "--out", str(d19), "--limit", "1")
    record("exit 0", rc == 0, f"rc={rc}")
    record("digits == 4", manifest(d19)["digits"] == 4, f"{manifest(d19)['digits']}")
    record("产出 page_0001.png", (d19 / "page_0001.png").is_file(), str(pngs(d19)))

    # ---------------- S20 dry-run 与正式运行一致 ----------------
    section("S20 dry-run 与正式运行的 skip 判定完全一致")
    d20 = WORK / "s20"
    shutil.copytree(d, d20)
    rc, out = run("--pdf", str(A), "--out", str(d20), "--dry-run")
    n_skip_line = out.count("将跳过")
    record("dry-run exit 0", rc == 0, f"rc={rc}")
    record("dry-run 判定 12 页全跳过", n_skip_line == 12, f"{n_skip_line}")
    before = {p: (d20 / p).stat().st_mtime_ns for p in pngs(d20)}
    rc, out2 = run("--pdf", str(A), "--out", str(d20))
    after = {p: (d20 / p).stat().st_mtime_ns for p in pngs(d20)}
    record("正式运行也 0 渲染", "渲染 0 页" in out2)
    record("dry-run 未写盘", before == after)

    (d20 / "page_006.png").write_bytes(b"")
    rc, out3 = run("--pdf", str(A), "--out", str(d20), "--dry-run")
    record("dry-run 对 0-byte 不再说跳过", out3.count("将跳过") == 11, f"{out3.count('将跳过')}")
    rc, out4 = run("--pdf", str(A), "--out", str(d20))
    record("正式运行同样只重渲 1 页（判定一致）", "渲染 1 页" in out4)

    # ---------------- 汇总 ----------------
    bad = [n for n, ok, _ in results if not ok]
    print("\n" + "=" * 64)
    print(f"合计 {len(results)} 项，通过 {len(results) - len(bad)}，失败 {len(bad)}")
    for n in bad:
        print(f"  - {n}")
    print("=" * 64)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
