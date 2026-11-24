"""应用服务层：枚举可选插件、组织两种顺序的推演结果。"""
from __future__ import annotations

import os

from .elfparse import is_x86_64_elf
from .resolver import simulate_both_orders

MAX_LISTED = 500


def list_plugins(mount_root: str) -> list[str]:
    """递归列出挂载目录内所有 x86-64 ELF 文件（相对路径，排序）。"""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(mount_root):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            if is_x86_64_elf(full):
                found.append(os.path.relpath(full, mount_root))
                if len(found) >= MAX_LISTED:
                    return found
    return found


def run_simulation(mount_root: str, rel_a: str, rel_b: str) -> dict:
    """校验输入并推演两种顺序，返回供模板渲染的结果字典。"""
    if not rel_a or not rel_b:
        raise ValueError("请选择两个插件")
    if rel_a == rel_b:
        raise ValueError("两个插件不能是同一个文件")
    plugins = set(list_plugins(mount_root))
    for rel in (rel_a, rel_b):
        if rel not in plugins:
            raise ValueError(f"不是挂载目录内的 x86-64 ELF：{rel}")
    return simulate_both_orders(mount_root, rel_a, rel_b)
