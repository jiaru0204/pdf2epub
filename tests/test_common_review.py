# -*- coding: utf-8 -*-
"""linefix.common 层回归 —— 覆盖 common.py review 里**本项目当前成立**的那些条。

跑法：cd D:/Project/pdf2epub && .venv/Scripts/python.exe tests/test_common_review.py
"""
from __future__ import annotations

import json
import shutil
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from linefix import common as C  # noqa: E402

WORK = ROOT / ".tmp" / "commontest"
results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))


def section(t: str) -> None:
    print(f"\n=== {t} ===")


# ---------------------------------------------------------------- 1. fail closed
def test_fingerprint_fail_closed() -> None:
    section("① ocr_fingerprint 算不出来 → 必须判为不可复用（fail closed）")
    cfg = ROOT / "configs" / "rapidocr_openvino_ppocrv6_medium.yaml"
    bad = WORK / "broken.yaml"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("Global: [oops\n", encoding="utf-8")

    want_bad = C.ocr_fingerprint(bad)
    record("坏配置留下 _cfg_error 标记", "_cfg_error" in want_bad, str(want_bad.get("_cfg_error"))[:60])

    # 旧实现会跳过 `_` 键 → 返回空列表 → 被读成"一致" → 缓存命中。
    stored = {"product_schema": 5}          # 假装是旧产物里的 provenance
    diffs = C.fingerprint_drift(stored, want_bad)
    record("fingerprint_drift 必须返回非空（绝不能被读成一致）", len(diffs) > 0, str(diffs))
    record("差异原因指向'无法计算'", any("无法计算" in d for d in diffs), str(diffs))

    # 正常配置不受影响
    want_ok = C.ocr_fingerprint(cfg)
    record("正常配置没有 _cfg_error", "_cfg_error" not in want_ok)
    record("正常配置对同一份 stored 也是非空差异（因为字段不全）",
           len(C.fingerprint_drift(stored, want_ok)) > 0)

    # 真的对得上时必须返回空（别把所有情况都判成不一致）
    same = dict(want_ok)
    record("stored 与 want 完全一致 → 返回空", C.fingerprint_drift(same, want_ok) == [])


def test_backend_triple() -> None:
    section("② 三种 backend 语义必须能区分：配置要求 / CLI 覆盖 / 实际生效")
    cfg = ROOT / "configs" / "rapidocr_openvino_ppocrv6_medium.yaml"
    a = C.ocr_fingerprint(cfg, backend="auto")
    b = C.ocr_fingerprint(cfg, backend="onnxruntime")
    record("auto：config=openvino，override=auto，生效=openvino",
           (a["backend_config"], a["backend_override"], a["backend"]) == ("openvino", "auto", "openvino"),
           f"{a['backend_config']} / {a['backend_override']} / {a['backend']}")
    record("覆盖：config 仍记 openvino，override/生效=onnxruntime",
           (b["backend_config"], b["backend_override"], b["backend"]) == ("openvino", "onnxruntime", "onnxruntime"),
           f"{b['backend_config']} / {b['backend_override']} / {b['backend']}")
    d = C.fingerprint_drift(a, b)
    record("两者指纹不同（backend 参与比对）",
           any(x.startswith("backend:") for x in d) and any(x.startswith("backend_override") for x in d),
           str(d))


# ---------------------------------------------------------------- 2. 原子写
def test_atomic_write() -> None:
    section("③ 原子写：唯一临时名 + 不残留 + 并发不撕裂")
    d = WORK / "atomic"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    target = d / "page_001.png"

    C.atomic_write_bytes(target, b"hello")
    record("写成功", target.read_bytes() == b"hello")
    record("目录里没有残留 *.tmp", list(d.glob("*.tmp")) == [], str(list(d.glob("*"))))

    # 临时文件名必须唯一：截获 os.replace，看它拿到的源文件名
    import os as _os
    seen: list[str] = []
    real_replace = C.os.replace

    def spy(src, dst):
        seen.append(Path(src).name)
        return real_replace(src, dst)

    C.os.replace = spy
    try:
        C.atomic_write_bytes(target, b"x")
    finally:
        C.os.replace = real_replace
    name = seen[0] if seen else ""
    record("临时名是隐藏 dotfile（不会被 page_*.png / *.png 扫到）", name.startswith("."), name)
    record("临时名含 pid（跨进程唯一）", str(_os.getpid()) in name, name)
    record("临时名含线程 id（同进程跨线程唯一）", str(threading.get_ident()) in name, name)
    record("临时名以 .tmp 结尾", name.endswith(".tmp"), name)
    record("临时名不与被写的文件名完全相同", name != target.name, name)

    # 并发：两个进程/线程写同一路径。固定名 tmp 会互相覆盖 → 内容被撕裂。
    A = b"A" * (512 * 1024)
    B = b"B" * (300 * 1024)
    target.write_bytes(A)                 # 播种，保证后续内容只可能是 A / B（或撕裂）
    stop = threading.Event()
    torn: list[str] = []

    def writer(payload: bytes) -> None:
        while not stop.is_set():
            C.atomic_write_bytes(target, payload)

    th = [threading.Thread(target=writer, args=(A if i % 2 == 0 else B,), daemon=True)
          for i in range(6)]
    for t in th:
        t.start()
    for _ in range(400):
        try:
            data = target.read_bytes()
        except OSError:
            # Windows：写方 os.replace 的一瞬间，读方 open 会被拒（ERROR_ACCESS_DENIED）。
            # 这是平台行为不是数据问题 —— 只对**成功读到**的内容下判断。
            continue
        if data != A and data != B:
            torn.append(f"len={len(data)}")
            break
    stop.set()
    for t in th:
        t.join(timeout=5)
    record("并发写入过程中**成功读到**的永远是完整的某一次写入（无撕裂）", not torn, str(torn[:2]))
    record("并发结束后文件仍是完整内容", target.read_bytes() in (A, B),
           f"len={len(target.read_bytes())}")
    record("并发后无残留临时文件", list(d.glob("*.tmp")) == [], str(list(d.glob('*'))[:5]))


# ---------------------------------------------------------------- 3. 序列化
def test_product_json() -> None:
    section("④ 紧凑序列化：逐点数组压行 + 内容必须逐值等价")
    d = WORK / "json"
    d.mkdir(parents=True, exist_ok=True)

    doc = {
        "page": "page_001",
        "lines": [
            {"line_id": 1, "bbox": [1, 2, 3, 4], "text": "中文字符",
             "top2": [[0, 5, 0.99, "己", 0.001], [1, 6, 1.0, "己", 0.0]]},
            {"line_id": 2, "bbox": [5, 6, 7, 8], "text": "e\u0301moji 🀄 テスト",
             "top2": []},
        ],
        "det_boxes": [[1, 2, 3, 4], [5, 6, 7, 8]],
        "dropped_lines": [{"det_index": 2, "text": "空", "score": 0.1, "top2": None}],
        "nested": {"a": {"b": [1, {"c": "d"}]}},
        "none_field": None,
        "empty_list": [],
    }
    s = C.dumps_product_json(doc)
    back = json.loads(s)
    record("round-trip 逐值等价", back == json.loads(json.dumps(doc, ensure_ascii=False)))
    record("top2 被压成单行",
           sum(1 for ln in s.splitlines() if '"top2"' in ln and "[[" in ln) >= 1)
    n_raw = len(json.dumps(doc, ensure_ascii=False, indent=1).splitlines())
    record(f"行数显著少于默认缩进（{len(s.splitlines())} vs {n_raw}）",
           len(s.splitlines()) < n_raw, f"{len(s.splitlines())} vs {n_raw}")
    record("空 top2 / 空列表 / None 字段都不出错", "empty_list" in s and "null" in s)

    # 落盘 + 读回
    p = d / "p.textlines.json"
    C.write_product_json(p, doc)
    record("write_product_json 落盘后能读回", json.loads(p.read_text(encoding="utf-8")) == back)
    record("落盘无残留 tmp", list(d.glob("*.tmp")) == [])

    # 🔴 哨兵碰撞：正文里出现与哨兵**同形**的转义序列时，必须**拒绝落盘**而不是写坏
    collide = {
        "lines": [{"line_id": 1, "bbox": [0, 0, 1, 1], "text": "\x00P\x000\x00",
                   "top2": [[0, 0, 0.9, "x", 0.1]]}],
        "det_boxes": [[0, 0, 1, 1]],
    }
    try:
        C.dumps_product_json(collide)
        record("哨兵碰撞被自校验挡住（拒绝落盘）", False, "居然没抛错 → 产物会被悄悄改坏")
    except RuntimeError as e:
        record("哨兵碰撞被自校验挡住（拒绝落盘）", True, str(e)[:60])
    except Exception as e:
        record("哨兵碰撞被自校验挡住（拒绝落盘）", False, f"抛了 {type(e).__name__}: {e}")


# ---------------------------------------------------------------- 4. 页图身份
def test_image_drift() -> None:
    section("⑤ image_drift：'页图不存在' 绝不能返回 None（None 只能表示'一致'）")
    from PIL import Image
    d = WORK / "img"
    d.mkdir(parents=True, exist_ok=True)
    png = d / "page_002.png"
    Image.new("RGB", (80, 60), "white").save(png)

    prov = C.image_fingerprint(png)
    record("页图未变 → None", C.image_drift(prov, png) is None)
    record("产物没有 provenance → 非 None", C.image_drift(None, png) is not None)
    record("产物没记哈希 → 非 None", C.image_drift({"png_bytes": 1}, png) is not None)
    record("页图字节数变了 → 非 None",
           C.image_drift({**prov, "png_bytes": 1}, png) is not None)
    record("页图内容变了 → 非 None",
           C.image_drift({**prov, "png_sha256": "0" * 64}, png) is not None)
    miss = C.image_drift(prov, d / "nope.png")
    record("页图不存在 → 非 None", miss is not None, repr(miss))
    record("缺图的说明里点明'不存在'", "不存在" in (miss or ""), repr(miss))


# ---------------------------------------------------------------- 5. 图版页判据
def test_figure_page() -> None:
    section("⑥ is_figure_page：判不了必须是 None，不能是 False")
    from PIL import Image, ImageDraw
    d = WORK / "fig"
    d.mkdir(parents=True, exist_ok=True)

    white = d / "white.png"
    Image.new("RGB", (100, 100), "white").save(white)
    record("白页 → False", C.is_figure_page(white) is False)

    dark = d / "dark.png"
    img = Image.new("RGB", (100, 100), "black")
    ImageDraw.Draw(img).rectangle([10, 10, 90, 90], fill="black")
    img.save(dark)
    record("暗页 → True", C.is_figure_page(dark) is True)

    record("文件不存在 → None（不是 False）", C.is_figure_page(d / "nope.png") is None)

    corrupt = d / "corrupt.png"
    corrupt.write_bytes(b"not an image at all")
    record("损坏图片 → None（不是 False）", C.is_figure_page(corrupt) is None)

    cache: dict = {}
    C.is_figure_page(white, cache=cache)
    n_before = len(cache)
    C.is_figure_page(white, cache=cache)
    record("cache 命中（第二次不再重算）", len(cache) == n_before == 1, str(n_before))
    C.is_figure_page(corrupt, cache=cache)
    record("None 也会被缓存（不重复尝试解码坏图）", len(cache) == 2, str(len(cache)))
    record("cache key 做了绝对路径归一化",
           all(Path(k).is_absolute() for k in cache), str(list(cache))[:120])


# ---------------------------------------------------------------- 6. OutputLock
def test_output_lock() -> None:
    section("⑦ OutputLock（两阶段共用）")
    d = WORK / "lock"
    d.mkdir(parents=True, exist_ok=True)
    a = C.OutputLock(d / ".x.lock")
    b = C.OutputLock(d / ".x.lock")
    record("第一个拿到锁", a.acquire() is True)
    record("第二个被挡住", b.acquire() is False)
    a.release()
    record("释放后第二个能拿到", b.acquire() is True)
    b.release()
    record("重复 release 不炸", (b.release(), True)[1])


# ---------------------------------------------------------------- 8. 本轮新补的函数
def test_aligned_additions() -> None:
    section("⑧ 对齐后新增的函数：契约与严格校验")

    import numpy as np
    d = WORK / "aligned"
    d.mkdir(parents=True, exist_ok=True)

    # ---- imwrite_u：必须原子（与 atomic_write_bytes 同一套约定）----
    img = np.zeros((40, 60, 3), dtype=np.uint8)
    img[10:30, 15:45] = 255
    target = d / "中文路径-图.png"
    seen: list[str] = []
    real_replace = C.os.replace

    def spy(src, dst):
        seen.append(Path(src).name)
        return real_replace(src, dst)

    C.os.replace = spy
    try:
        ok = C.imwrite_u(target, img)
    finally:
        C.os.replace = real_replace
    record("imwrite_u 中文路径写成功", ok is True and target.is_file(), str(ok))
    record("imwrite_u 走的是 tmp→replace（原子）", bool(seen) and seen[0].endswith(".tmp"), str(seen))
    record("imwrite_u 临时名是隐藏 dotfile", bool(seen) and seen[0].startswith("."), str(seen))
    record("imwrite_u 无残留 tmp", list(d.glob("*.tmp")) == [], str(list(d.glob("*"))))
    record("imwrite_u 写出来的图能读回（尺寸一致）",
           tuple(C.imread_u(target).shape[:2]) == (40, 60), str(C.imread_u(target).shape))
    record("page_size 读到尺寸", C.page_size(target) == (60, 40), str(C.page_size(target)))

    # ---- read_lines：坏 schema 必须返回 None，不能抛异常 ----
    p = d / "rl.json"
    for name, payload in [
        ('{"lines": {}}', "lines 是 dict"),
        ('{"lines": null}', "lines 是 null"),
        ('{"lines": "abc"}', "lines 是字符串"),
        ('{"lines": [1, 2]}', "lines 元素不是 dict"),
        ('[1,2,3]', "顶层不是 dict"),
    ]:
        p.write_text(payload, encoding="utf-8")
        try:
            got = C.read_lines(p)
        except Exception as e:
            record(f"read_lines({name}) 不抛异常", False, f"{type(e).__name__}: {e}")
            continue
        record(f"read_lines 坏输入返回 None（{name}）", got is None, repr(got)[:60])

    p.write_text('{"lines": [{"line_id": 1, "bbox": [0,0,1,1], "text": "a"}]}', encoding="utf-8")
    got = C.read_lines(p)
    record("read_lines 合法输入返回 dict", isinstance(got, dict))
    record("合法输入补齐 orig_line_id / added",
           got["lines"][0]["orig_line_id"] == 1 and got["lines"][0]["added"] is False)
    record("read_lines 文件不存在 → None", C.read_lines(d / "nope.json") is None)

    # ---- sort_lines_like_engine：bbox 契约 ----
    good = [{"bbox": [0, 100, 10, 5]}, {"bbox": [50, 100, 10, 5]}, {"bbox": [0, 300, 10, 5]}]
    order = C.sort_lines_like_engine(good)
    record("同 y 的两行按 x 排，换行按 y",
           [L["bbox"][0] for L in order] == [0, 50, 0], str([L["bbox"] for L in order]))
    for bad_bbox, why in [([None, 1, 2], "bbox=null"),
                          ([1], "bbox 只有 1 个元素"),
                          ("abcd", "bbox 是字符串")]:
        try:
            C.sort_lines_like_engine([{"bbox": bad_bbox}])
            record(f"{why} → 响亮抛错", False, "没抛错")
        except ValueError as e:
            record(f"{why} → 响亮抛错且指出是第几行", True, str(e)[:50])
        except Exception as e:
            record(f"{why} → 响亮抛错（但类型不是 ValueError）", False, f"{type(e).__name__}: {e}")
    try:
        C.sort_lines_like_engine([{"bbox": [0, 0, 1, 1]}, {"bbox": None}])
        record("能定位到出错的元素下标", False)
    except ValueError as e:
        record("能定位到出错的元素下标", "lines[1]" in str(e), str(e)[:50])

    # ---- iter_alts：两代口径 ----
    new = {"text": "我们", "top2": [[0, 5, 0.9, "己", 0.05], [1, 6, 1.0, "门", 0.0]]}
    old = {"text": "我们", "alt_candidates": [{"i": 0, "t": 5, "c1": "我", "p1": 0.9, "c2": "己", "p2": 0.05}]}
    record("新口径 top2 归一化（c1 取自 text[i]）",
           [a["c1"] for a in C.iter_alts(new)] == ["我", "们"], str(list(C.iter_alts(new))))
    record("旧口径 alt_candidates 原样透出", list(C.iter_alts(old))[0]["i"] == 0)
    record("越界的 i 被丢掉", list(C.iter_alts({"text": "我", "top2": [[9, 1, 0.9, "x", 0.1]]})) == [])
    record("两种口径都没有 → 空", list(C.iter_alts({"text": "我"})) == [])

    # ---- write_lines round-trip ----
    wp = d / "wl.json"
    C.write_lines(wp, "page_007", [{"line_id": 1, "bbox": [0, 0, 1, 1], "text": "字"}],
                  {"k": "v"}, orig_line_count=1, added=0,
                  det_boxes=[[0, 0, 1, 1]], dropped_lines=[])
    back = C.read_lines(wp)
    record("write_lines → read_lines 往返",
           back["page"] == "page_007" and back["det_boxes"] == [[0, 0, 1, 1]]
           and back["dropped_lines"] == [], str(sorted(back)))
    record("write_lines 透传 orig_line_id/added 默认值",
           back["lines"][0]["orig_line_id"] == 1 and back["lines"][0]["added"] is False)

    # ---- inc ----
    st: dict = {}
    C.inc(st, "a"); C.inc(st, "a"); C.inc(st, "b", 5)
    record("inc 记账正确", st == {"a": 2, "b": 5}, str(st))

    # ---- char_x_spans：口径说明与退化行为 ----
    spans = C.char_x_spans_even([0, 0, 100, 10], 4)
    record("char_x_spans_even 等分", spans == [(0, 25), (25, 50), (50, 75), (75, 100)], str(spans))
    record("非单调 cols → None（不拿去裁图）",
           C.char_x_spans_from_cols([0, 0, 100, 10], [5, 3, 8]) is None)
    rec = C.char_x_spans_from_cols([0, 0, 100, 10], [0, 4, 8])
    record("端点标定：首尾被钉在框上",
           rec is not None and rec[0][0] == 0.0 and rec[-1][1] == 100.0, str(rec))


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    test_fingerprint_fail_closed()
    test_backend_triple()
    test_atomic_write()
    test_product_json()
    test_image_drift()
    test_figure_page()
    test_output_lock()
    test_aligned_additions()
    bad = [n for n, ok, _ in results if not ok]
    print("\n" + "=" * 66)
    print(f"合计 {len(results)} 项，通过 {len(results) - len(bad)}，失败 {len(bad)}")
    for n in bad:
        print(f"  - {n}")
    print("=" * 66)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
