# -*- coding: utf-8 -*-
"""产线各阶段。按执行顺序：

- `split_pages`   PDF → 逐页 PNG（300 DPI）。整条产线的第一环。
- `ocr`           页图 → 逐页文本行产物（`*.textlines.json`，含过滤前 `det_boxes`）。
                  第二环，也是后续「漏行审计」的数据来源。
"""
