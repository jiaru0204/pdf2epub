# -*- coding: utf-8 -*-
"""跨阶段共用的实现层（**当前只含拆页阶段用到的部分**）。

口径只写一份：原子写盘 / 文件哈希这类"所有阶段都要用、且必须完全一致"的实现，
统一放这里，不许各阶段各写一套。

已就位：
    atomic_write_bytes / atomic_write_text —— 原子写盘
    file_sha256                            —— 文件内容哈希

说明：参考实现里这个模块还有图像指纹、产物 JSON 读写、滤镜判据、引擎指纹等内容，
那些属于后续阶段（OCR / 漏行审计），**本阶段不预置**——用到再加，避免留下一堆没人
调用的接口。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Union


# ---------------------------------------------------------------------------
# 落盘：**原子写**是全局约定（半截文件绝不能留给下游）
# ---------------------------------------------------------------------------
def atomic_write_bytes(path: Union[str, Path], data: bytes) -> None:
    """原子写二进制：先写 `<名>.tmp` 再 `os.replace`。

    ⚠️ 为什么不能省：半截 JSON 的危害不是"多一个坏文件"，而是**下游可能把它当合法产物**。
    本项目所有写盘（页图、产物、清单）都走这里，口径只写一份。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, p)


def atomic_write_text(path: Union[str, Path], text: str) -> None:
    """原子写文本（UTF-8）。"""
    atomic_write_bytes(path, text.encode("utf-8"))


def file_sha256(path: Union[str, Path], chunk: int = 1 << 20) -> str:
    """文件内容哈希。用于"这份产物对应的到底是哪一版输入"这类判定。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()
