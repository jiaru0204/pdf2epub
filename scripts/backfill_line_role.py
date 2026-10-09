# -*- coding: utf-8 -*-
"""一次性迁移：给已有 OCR 产物补 `lines[].role`（PRODUCT_SCHEMA 5 → 6）。

跑法：`.venv/Scripts/python.exe scripts/backfill_line_role.py [--dry-run]`

为什么可以「补写」而不必重跑 OCR
----------------------------------------------------------------
`role` 只是 `(text, bbox, 页图高度)` 的**纯函数**（判据在
`linefix.common.page_number_role`），不依赖任何 OCR 推理结果 ——
补写调用的就是 `pipeline/ocr.py` 用的同一个函数，所以结果与重新 OCR 逐字段一致
（已用 `--force` 重跑抽样页核对过）。这省掉整本 58 分钟的重算。

⚠️ 本脚本只做三件事，**不碰任何指纹字段**：
  1. 给每行补 `role`；
  2. 把 `provenance.product_schema` 从 5 改成 6（否则下次跑会被判过期而整本重跑）；
  3. 记 `provenance.page_number_lines`。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from linefix import common as C          # noqa: E402

OUT_DIR = ROOT / "orign_pdf" / "textlines"
PAGES_DIR = ROOT / "orign_pdf" / "pages"
PAGES_DIR_ALT = OUT_DIR.parent / "pages"
MANIFEST = OUT_DIR / "ocr_manifest.json"
TARGET = 6


def main() -> int:
    ap = argparse.ArgumentParser(description="补写 lines[].role（PRODUCT_SCHEMA 5 → 6）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写盘")
    args = ap.parse_args()

    pages_dir = PAGES_DIR if PAGES_DIR.is_dir() else PAGES_DIR_ALT
    files = sorted(OUT_DIR.glob("*.textlines.json"))
    if not files:
        print(f"✗ {OUT_DIR} 下没有产物")
        return 2

    n_done = n_skip = n_nosize = n_pageno = 0
    bad: list[str] = []
    for f in files:
        name = f.name[: -len(".textlines.json")]
        d = C.load_json(f)
        if not isinstance(d, dict) or "lines" not in d:
            bad.append(name)
            continue
        prov = d.get("provenance") or {}
        if prov.get("product_schema") == TARGET and all("role" in ln for ln in d["lines"]):
            n_skip += 1
            continue
        sz = C.page_size(pages_dir / f"{name}.png")
        if sz is None:
            n_nosize += 1
        page_h = sz[1] if sz else 0
        cnt = 0
        for ln in d["lines"]:
            r = C.page_number_role(ln.get("text", ""), ln.get("bbox"), page_h)
            ln["role"] = r
            cnt += int(r == "page_number")
        # 只改这三个键，指纹字段（png_sha256 / 版本 / 模型哈希 …）一律不动
        prov["product_schema"] = TARGET
        prov["page_number_lines"] = cnt
        d["provenance"] = prov
        n_pageno += cnt
        if not args.dry_run:
            C.write_product_json(f, d)
        n_done += 1

    # 清单同步（复用判据不看它，但不改会读起来自相矛盾）
    man = C.load_json(MANIFEST)
    if isinstance(man, dict) and not args.dry_run:
        p = man.get("provenance") or {}
        p["product_schema"] = TARGET
        man["provenance"] = p
        b = man.get("book") or {}
        b["page_number_lines"] = n_pageno
        man["book"] = b
        C.dump_json(MANIFEST, man)

    print(f"{'[dry-run] ' if args.dry_run else ''}产物 {len(files)} 个："
          f"补写 {n_done}｜已是 v{TARGET} 跳过 {n_skip}｜读不到页图尺寸 {n_nosize}")
    print(f"标记为页码的行共 {n_pageno} 行")
    if bad:
        print(f"⚠ 解析失败 {len(bad)} 个：{bad[:8]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
