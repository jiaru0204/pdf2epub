# -*- coding: utf-8 -*-
"""linefix —— 本项目的共用实现层。

**当前只有 `common`**（口径唯一来源：原子写、产物 JSON 序列化、页图身份/过期判定、
引擎构造与缓存指纹、输出目录锁、几何与判据）。拆页与 OCR 两个阶段都 import 它。

⚠️ 后续阶段（漏行审计 `audit` / 局部救援 `rescue` / 字级复核 `recheck`）
**尚未移植** —— 需要时再加，不要预先搭空壳。`pipeline/ocr.py` 产出的
`det_boxes` / `dropped_lines` / `top2` 就是为那几个阶段准备的数据。
"""
