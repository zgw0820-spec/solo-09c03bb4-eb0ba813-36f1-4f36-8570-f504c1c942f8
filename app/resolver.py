"""加载推演引擎：在同一进程语义下，按给定顺序模拟两个插件的依赖解析。

规则（与需求一一对应）：
  * 不执行任何插件 / 库，只读取 ELF 元数据；
  * 每种顺序都从“尚未装入任何私有库”的空状态独立推演；
  * 按 DT_NEEDED 的顺序逐层（广度优先）找库；
  * 引用方用自己的 DT_RUNPATH 找首个同名文件，且只支持 $ORIGIN 相对目录；
  * 同名私有库一旦装入，后续引用只能复用这一份；
  * 只支持文件名与 SONAME 一致的私有库；
  * 固定名单内的系统库直接跳过，不做任何核查；
  * 每条私有依赖边都核对引用方要求的 GNU 版本化强符号；
  * 安全约束：所有路径必须落在挂载目录内。
"""
from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field

from .elfparse import ElfError, ElfFile

# 固定名单的系统库：命中即跳过，不找文件、不核查。
SYSTEM_LIBS = frozenset({
    "ld-linux-x86-64.so.2",
    "libc.so.6",
    "libm.so.6",
    "libdl.so.2",
    "librt.so.1",
    "libpthread.so.0",
    "libresolv.so.2",
    "libutil.so.1",
    "libgcc_s.so.1",
    "libstdc++.so.6",
    "libnss_files.so.2",
    "libnss_dns.so.2",
})


class Block(Exception):
    """推演阻断。携带依赖链、原因与（可选的）已命中库路径。"""

    def __init__(self, reason: str, chain: list[dict], hit_path: str | None = None,
                 missing: list[tuple[str, str]] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.chain = chain          # [{"name": ..., "path": ...或None}, ...]
        self.hit_path = hit_path
        self.missing = missing or []


@dataclass
class LoadedLib:
    name: str                      # SONAME（== 文件名）；根插件无 SONAME 时用文件名
    path: str                      # 绝对路径
    elf: ElfFile
    is_root: bool = False
    chain: list = field(default_factory=list)  # 从根插件到本库的链


def _expand_origin(entry: str, origin_dir: str) -> str | None:
    """展开 $ORIGIN 相对目录；不支持的写法返回 None。"""
    for token in ("$ORIGIN", "${ORIGIN}"):
        if entry == token:
            return origin_dir
        if entry.startswith(token + "/"):
            return os.path.normpath(origin_dir + entry[len(token):])
    return None


def _within(root: str, path: str) -> bool:
    try:
        return os.path.commonpath((root, path)) == root
    except ValueError:
        return False


def _find_in_runpath(referrer: LoadedLib, needed: str, mount_root: str,
                     events: list[str]) -> str:
    """用引用方自己的 DT_RUNPATH（仅 $ORIGIN 相对目录）找首个同名文件。"""
    origin_dir = os.path.dirname(referrer.path)
    searched: list[str] = []
    unsupported: list[str] = []
    for entry in referrer.elf.runpath:
        resolved = _expand_origin(entry, origin_dir)
        if resolved is None:
            unsupported.append(entry)
            continue
        if not _within(mount_root, os.path.realpath(resolved)):
            raise Block(
                f"DT_RUNPATH 条目 {entry!r} 展开后越出挂载目录，拒绝继续",
                chain=referrer.chain + [{"name": needed, "path": None}],
            )
        searched.append(resolved)
        candidate = os.path.join(resolved, needed)
        if os.path.isfile(candidate):
            if unsupported:
                events.append(
                    f"忽略不支持的 DT_RUNPATH 条目（仅支持 $ORIGIN 相对目录）："
                    f"{', '.join(unsupported)}")
            return candidate
    detail = f"引用方 {referrer.name} 的 DT_RUNPATH 中找不到 {needed}"
    if searched:
        detail += "；已按序查找目录：" + "、".join(searched)
    else:
        detail += "；DT_RUNPATH 中没有可用的 $ORIGIN 相对目录"
    if unsupported:
        detail += "；不支持的条目（已跳过）：" + "、".join(unsupported)
    raise Block(detail, chain=referrer.chain + [{"name": needed, "path": None}])


def _load_private(referrer: LoadedLib, needed: str, path: str,
                  mount_root: str) -> LoadedLib:
    """解析候选私有库并做装入前检查（x86-64、SONAME==文件名）。"""
    try:
        elf = ElfFile(path)
    except ElfError as exc:
        raise Block(f"候选库 {path} 不是可用的 x86-64 ELF：{exc}",
                    chain=referrer.chain + [{"name": needed, "path": path}],
                    hit_path=path)
    if elf.soname != needed:
        raise Block(
            f"私有库 {path} 的 SONAME（{elf.soname or '无'}）与文件名 "
            f"{needed} 不一致，不在支持范围",
            chain=referrer.chain + [{"name": needed, "path": path}],
            hit_path=path)
    return LoadedLib(name=needed, path=path, elf=elf,
                     chain=referrer.chain + [{"name": needed, "path": path}])


def _check_versioned_symbols(referrer: LoadedLib, provider: LoadedLib) -> None:
    """核对引用方对这条私有依赖要求的 GNU 版本化强符号。"""
    missing: list[tuple[str, str]] = []
    for sym, ver in referrer.elf.versioned_requirements(provider.name):
        provided = provider.elf.strong_provided_versions(sym)
        if ver not in provided:
            missing.append((sym, ver))
    if missing:
        pretty = "、".join(f"{s}@{v}" for s, v in missing)
        raise Block(
            f"已装入的 {provider.name} 缺少引用方 {referrer.name} 要求的 "
            f"GNU 版本化强符号：{pretty}",
            chain=referrer.chain + [{"name": provider.name,
                                     "path": provider.path}],
            hit_path=provider.path,
            missing=missing)

def simulate_order(plugin_paths: list[str], mount_root: str) -> dict:
    """从空状态推演一种装入顺序。plugin_paths 为绝对路径。"""
    mount_root = os.path.realpath(mount_root)
    loaded: dict[str, LoadedLib] = {}
    loads: list[dict] = []     # 实际装入的库（含根插件），按装入先后
    events: list[str] = []     # 过程日志（复用 / 跳过系统库等）
    block: Block | None = None

    def record_load(lib: LoadedLib, via: str | None, reused: bool) -> None:
        loads.append({"name": lib.name, "path": lib.path, "via": via,
                      "reused": reused, "is_root": lib.is_root})

    try:
        for plugin_path in plugin_paths:
            try:
                elf = ElfFile(plugin_path)
            except ElfError as exc:
                raise Block(f"插件 {plugin_path} 不是可用的 x86-64 ELF：{exc}",
                            chain=[{"name": os.path.basename(plugin_path),
                                    "path": plugin_path}],
                            hit_path=plugin_path)
            root_name = elf.soname or os.path.basename(plugin_path)
            root = LoadedLib(name=root_name, path=plugin_path, elf=elf,
                             is_root=True,
                             chain=[{"name": root_name, "path": plugin_path}])
            loaded.setdefault(root_name, root)
            record_load(root, via=None, reused=False)

            queue: deque[tuple[LoadedLib, str]] = deque(
                (root, n) for n in elf.needed)
            while queue:
                referrer, needed = queue.popleft()
                if needed in SYSTEM_LIBS:
                    events.append(f"{referrer.name} 需要 {needed}："
                                  f"固定名单内的系统库，跳过核查")
                    continue
                if needed in loaded:
                    provider = loaded[needed]
                    events.append(f"{referrer.name} 需要 {needed}："
                                  f"同名私有库已装入，复用 {provider.path}")
                    record_load(provider, via=referrer.name, reused=True)
                else:
                    path = _find_in_runpath(referrer, needed, mount_root,
                                            events)
                    provider = _load_private(referrer, needed, path,
                                             mount_root)
                    loaded[needed] = provider
                    events.append(f"{referrer.name} 需要 {needed}："
                                  f"装入 {path}")
                    record_load(provider, via=referrer.name, reused=False)
                    for n in provider.elf.needed:
                        queue.append((provider, n))
                _check_versioned_symbols(referrer, provider)
    except Block as b:
        block = b

    return {
        "order": list(plugin_paths),
        "status": "blocked" if block else "ok",
        "loads": loads,
        "events": events,
        "block": None if block is None else {
            "reason": block.reason,
            "chain": block.chain,
            "hit_path": block.hit_path,
            "missing": block.missing,
        },
    }


def simulate_both_orders(mount_root: str, rel_a: str, rel_b: str) -> dict:
    """对两个插件分别推演 A→B 与 B→A 两种顺序（各自从空状态开始）。"""
    path_a = resolve_inside(mount_root, rel_a)
    path_b = resolve_inside(mount_root, rel_b)
    return {
        "mount_root": os.path.realpath(mount_root),
        "plugin_a": rel_a,
        "plugin_b": rel_b,
        "orders": [
            simulate_order([path_a, path_b], mount_root),
            simulate_order([path_b, path_a], mount_root),
        ],
    }


def resolve_inside(mount_root: str, rel: str) -> str:
    """把挂载目录内的相对路径解析为绝对路径，拒绝越界。"""
    root = os.path.realpath(mount_root)
    path = os.path.realpath(os.path.join(root, rel))
    if not _within(root, path):
        raise ElfError(f"路径越出挂载目录：{rel}")
    if not os.path.isfile(path):
        raise ElfError(f"文件不存在：{rel}")
    return path
