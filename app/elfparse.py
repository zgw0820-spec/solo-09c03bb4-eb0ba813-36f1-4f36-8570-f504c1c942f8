"""只读的最小 ELF64 解析器，面向 x86-64 共享库 / 插件。

只提取加载推演需要的信息：
  - 动态标签 DT_NEEDED / DT_RUNPATH / DT_SONAME
  - 动态符号表 .dynsym
  - GNU 符号版本节 .gnu.version / .gnu.version_d / .gnu.version_r

文件只被读取，绝不执行。解析失败抛出 ElfError。
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field


class ElfError(Exception):
    """文件不是可支持的 ELF 子集，或内容损坏。"""


# --- ELF 常量 ---------------------------------------------------------------
EI_CLASS = 4
EI_DATA = 5
ELFCLASS64 = 2
ELFDATA2LSB = 1
EM_X86_64 = 62

PT_LOAD = 1
PT_DYNAMIC = 2

SHT_DYNAMIC = 6
SHT_DYNSYM = 11
SHT_GNU_VERSYM = 0x6FFFFFFF
SHT_GNU_VERDEF = 0x6FFFFFFD
SHT_GNU_VERNEED = 0x6FFFFFFE

SHN_UNDEF = 0
STB_GLOBAL = 1
STB_WEAK = 2

DT_NEEDED = 1
DT_STRTAB = 5
DT_STRSZ = 10
DT_SONAME = 14
DT_RUNPATH = 29

VERSYM_HIDDEN = 0x8000
VERSYM_IDX_MASK = 0x7FFF

MAX_FILE_SIZE = 256 * 1024 * 1024  # 防御性上限

_EHDR = struct.Struct("<16sHHIQQQIHHHHHH")
_PHDR = struct.Struct("<IIQQQQQQ")
_SHDR = struct.Struct("<IIQQQQIIQQ")
_DYN = struct.Struct("<qQ")
_SYM = struct.Struct("<IBBHQQ")
_VERDEF = struct.Struct("<HHHHIII")
_VERDAUX = struct.Struct("<II")
_VERNEED = struct.Struct("<HHIII")
_VERNAUX = struct.Struct("<IHHII")


def _unpack_at(st: struct.Struct, data: bytes, off: int, what: str):
    if off < 0 or off + st.size > len(data):
        raise ElfError(f"文件截断：读取 {what}（偏移 {off}）越界")
    return st.unpack_from(data, off)


def _cstr(data: bytes, off: int, what: str) -> str:
    if off < 0 or off >= len(data):
        raise ElfError(f"字符串越界：{what}")
    end = data.find(b"\x00", off)
    if end < 0:
        raise ElfError(f"字符串未终结：{what}")
    try:
        return data[off:end].decode("utf-8", "replace")
    except Exception as exc:  # pragma: no cover - 防御
        raise ElfError(f"字符串解码失败：{what}: {exc}")


@dataclass
class Section:
    name: str
    sh_type: int
    offset: int
    size: int
    link: int
    entsize: int


@dataclass
class DynSym:
    index: int
    name: str
    bind: int
    defined: bool

    @property
    def strong(self) -> bool:
        return self.bind == STB_GLOBAL


@dataclass
class VerNeed:
    file: str                      # vn_file：被要求的库文件名
    aux: list = field(default_factory=list)  # [(vna_other, 版本名)]


class ElfFile:
    """一个已解析的 x86-64 ELF 目标文件。"""

    def __init__(self, path: str):
        self.path = path
        self.soname: str | None = None
        self.needed: list[str] = []
        self.runpath: list[str] = []
        self.symbols: list[DynSym] = []
        self.versym: list[int] = []          # 每个 dynsym 的原始版本下标
        self.verdef: dict[int, str] = {}     # 版本下标 -> 版本名（本库定义）
        self.verneed: list[VerNeed] = []     # 本库（引用方）要求的版本
        self._parse()

    # ------------------------------------------------------------------ 解析
    def _parse(self) -> None:
        try:
            size = os.path.getsize(self.path)
        except OSError as exc:
            raise ElfError(f"无法访问文件：{exc}")
        if size > MAX_FILE_SIZE:
            raise ElfError(f"文件过大（{size} 字节），超出解析上限")
        try:
            with open(self.path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            raise ElfError(f"无法读取文件：{exc}")
        if len(data) < _EHDR.size:
            raise ElfError("文件太小，不是 ELF")

        (ident, e_type, e_machine, _e_version, _e_entry, e_phoff, e_shoff,
         _e_flags, _e_ehsize, e_phentsize, e_phnum, e_shentsize, e_shnum,
         _e_shstrndx) = _unpack_at(_EHDR, data, 0, "ELF 头")

        if ident[:4] != b"\x7fELF":
            raise ElfError("不是 ELF 文件")
        if ident[EI_CLASS] != ELFCLASS64 or ident[EI_DATA] != ELFDATA2LSB:
            raise ElfError("不是 64 位小端 ELF")
        if e_machine != EM_X86_64:
            raise ElfError(f"不是 x86-64（e_machine={e_machine}）")

        # 程序头：PT_LOAD（虚拟地址换算）与 PT_DYNAMIC
        loads = []          # (p_vaddr, p_offset, p_filesz)
        dyn_off = dyn_size = 0
        for i in range(e_phnum):
            off = e_phoff + i * e_phentsize
            (p_type, _flags, p_offset, p_vaddr, _paddr, p_filesz, _memsz,
             _align) = _unpack_at(_PHDR, data, off, f"程序头[{i}]")
            if p_type == PT_LOAD:
                loads.append((p_vaddr, p_offset, p_filesz))
            elif p_type == PT_DYNAMIC:
                dyn_off, dyn_size = p_offset, p_filesz
        if not dyn_off:
            raise ElfError("没有 PT_DYNAMIC 段（静态链接？）")

        def v2o(vaddr: int) -> int:
            for p_vaddr, p_offset, p_filesz in loads:
                if p_vaddr <= vaddr < p_vaddr + p_filesz:
                    return p_offset + (vaddr - p_vaddr)
            raise ElfError(f"虚拟地址 0x{vaddr:x} 不在任何 PT_LOAD 内")

        # 动态段
        strtab_off = 0
        needed_off: list[int] = []
        soname_off = runpath_off = None
        n = dyn_size // _DYN.size if dyn_size else 1 << 20
        for i in range(n):
            d_tag, d_val = _unpack_at(_DYN, data, dyn_off + i * _DYN.size,
                                      "动态段条目")
            if d_tag == 0:  # DT_NULL
                break
            if d_tag == DT_NEEDED:
                needed_off.append(d_val)
            elif d_tag == DT_STRTAB:
                strtab_off = v2o(d_val)
            elif d_tag == DT_SONAME:
                soname_off = d_val
            elif d_tag == DT_RUNPATH:
                runpath_off = d_val
        if not strtab_off:
            raise ElfError("动态段缺少 DT_STRTAB")

        def dynstr(off: int, what: str) -> str:
            return _cstr(data, strtab_off + off, what)

        self.needed = [dynstr(o, "DT_NEEDED") for o in needed_off]
        if soname_off is not None:
            self.soname = dynstr(soname_off, "DT_SONAME")
        if runpath_off is not None:
            raw = dynstr(runpath_off, "DT_RUNPATH")
            self.runpath = [p for p in raw.split(":") if p]

        # 节头：符号表与版本节
        sections = self._read_sections(data, e_shoff, e_shentsize, e_shnum)
        sec_by_type: dict[int, list[Section]] = {}
        for sec in sections:
            sec_by_type.setdefault(sec.sh_type, []).append(sec)

        def sec_strtab(sec: Section) -> tuple[int, int]:
            if sec.link >= len(sections):
                raise ElfError("节的字符串表链接无效")
            strsec = sections[sec.link]
            return strsec.offset, strsec.size

        dynsyms = sec_by_type.get(SHT_DYNSYM, [])
        if dynsyms:
            ds = dynsyms[0]
            str_off, str_size = sec_strtab(ds)

            def symstr(o: int, what: str) -> str:
                if o >= str_size:
                    raise ElfError(f"符号名越界：{what}")
                return _cstr(data, str_off + o, what)

            count = ds.size // (_SYM.size if ds.entsize == 0 else ds.entsize)
            for i in range(count):
                (st_name, st_info, _other, st_shndx, _val, _sz) = _unpack_at(
                    _SYM, data, ds.offset + i * _SYM.size, f"符号[{i}]")
                self.symbols.append(DynSym(
                    index=i,
                    name=symstr(st_name, f"符号[{i}]") if st_name else "",
                    bind=st_info >> 4,
                    defined=st_shndx != SHN_UNDEF,
                ))

            versyms = sec_by_type.get(SHT_GNU_VERSYM, [])
            if versyms:
                vs = versyms[0]
                for i in range(min(count, vs.size // 2)):
                    (val,) = _unpack_at(struct.Struct("<H"), data,
                                        vs.offset + i * 2, f"versym[{i}]")
                    self.versym.append(val)

            verdefs = sec_by_type.get(SHT_GNU_VERDEF, [])
            if verdefs:
                vd = verdefs[0]
                vstr_off, vstr_size = sec_strtab(vd)
                off = vd.offset
                end = vd.offset + vd.size
                while off and off < end:
                    (_ver, _flags, ndx, cnt, _hash, aux, nxt) = _unpack_at(
                        _VERDEF, data, off, "verdef")
                    if cnt > 0:
                        (name_off, _anext) = _unpack_at(
                            _VERDAUX, data, off + aux, "verdaux")
                        if name_off < vstr_size:
                            self.verdef[ndx] = _cstr(data, vstr_off + name_off,
                                                     "版本定义名")
                    if not nxt:
                        break
                    off += nxt

            verneeds = sec_by_type.get(SHT_GNU_VERNEED, [])
            if verneeds:
                vn = verneeds[0]
                vstr_off, vstr_size = sec_strtab(vn)
                off = vn.offset
                end = vn.offset + vn.size
                while off and off < end:
                    (_ver, cnt, file_off, aux, nxt) = _unpack_at(
                        _VERNEED, data, off, "verneed")
                    if file_off >= vstr_size:
                        raise ElfError("verneed 文件名越界")
                    entry = VerNeed(file=_cstr(data, vstr_off + file_off,
                                               "verneed 文件名"))
                    aoff = off + aux
                    for _ in range(cnt):
                        (_hash, _flags, other, name_off, anext) = _unpack_at(
                            _VERNAUX, data, aoff, "vernaux")
                        if name_off < vstr_size:
                            entry.aux.append(
                                (other, _cstr(data, vstr_off + name_off,
                                              "版本需求名")))
                        if not anext:
                            break
                        aoff += anext
                    self.verneed.append(entry)
                    if not nxt:
                        break
                    off += nxt

    @staticmethod
    def _read_sections(data: bytes, shoff: int, shentsize: int,
                       shnum: int) -> list[Section]:
        if not shoff or not shnum:
            return []
        sections: list[Section] = []
        for i in range(shnum):
            off = shoff + i * shentsize
            (_name, sh_type, _flags, _addr, sh_offset, sh_size, sh_link,
             _info, _align, sh_entsize) = _unpack_at(_SHDR, data, off,
                                                     f"节头[{i}]")
            sections.append(Section("", sh_type, sh_offset, sh_size, sh_link,
                                    sh_entsize))
        return sections

    # -------------------------------------------------------------- 版本语义
    def _versym_at(self, index: int) -> int:
        if index < len(self.versym):
            return self.versym[index] & VERSYM_IDX_MASK
        return 1  # 视为无版本

    def versioned_requirements(self, libname: str) -> list[tuple[str, str]]:
        """本文件作为引用方，对名为 libname 的库要求的 (符号, 版本) 列表。

        只统计未定义的强符号（bind==GLOBAL），弱符号与已定义符号不算。
        """
        reqs: list[tuple[str, str]] = []
        want: dict[int, str] = {}
        for vn in self.verneed:
            if vn.file != libname:
                continue
            for other, vname in vn.aux:
                want[other & VERSYM_IDX_MASK] = vname
        if not want:
            return reqs
        for sym in self.symbols:
            if sym.defined or not sym.strong:
                continue
            idx = self._versym_at(sym.index)
            if idx in want:
                reqs.append((sym.name, want[idx]))
        return reqs

    def strong_provided_versions(self, name: str) -> set[str | None]:
        """本文件作为提供方，名为 name 的强符号（已定义、GLOBAL）的版本名集合。

        集合中的 None 表示该符号存在无版本定义。
        """
        vers: set[str | None] = set()
        for sym in self.symbols:
            if sym.name != name or not sym.defined or not sym.strong:
                continue
            idx = self._versym_at(sym.index)
            vers.add(self.verdef.get(idx))  # 下标无定义 -> None（无版本）
        return vers


def is_x86_64_elf(path: str) -> bool:
    """廉价探测：只读文件头，判断是否为 x86-64 ELF。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(20)
    except OSError:
        return False
    if len(head) < 20 or head[:4] != b"\x7fELF":
        return False
    if head[EI_CLASS] != ELFCLASS64 or head[EI_DATA] != ELFDATA2LSB:
        return False
    machine = struct.unpack_from("<H", head, 18)[0]
    return machine == EM_X86_64
