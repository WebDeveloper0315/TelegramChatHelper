"""Reading what a shared library *is*, without loading it.

Two questions have to be answered before ``tdjson`` is mapped into this process,
and both are answerable from the file's own headers:

* **Which architecture is it?** A 32-bit library under a 64-bit interpreter
  fails to load with a message that names nothing useful. Reading the header
  first turns that into "this is x86, we are amd64".
* **What does it import?** The manifest checksums one file. Anything that file
  loads at runtime is inside the trust boundary and is *not* checked, so a
  ``tdjson`` importing OpenSSL or zlib is weaker than its digest suggests
  (ADR-047).

Parsed here rather than shelled out to ``dumpbin`` or ``ldd``. Those need a
toolchain present, differ per platform, and cannot be tested without one; a
handful of struct reads can be exercised with crafted fixtures on any machine.

Format support is deliberately uneven and says so. PE is parsed completely
because that is the platform this was verified on. ELF yields its architecture --
the header field is unambiguous -- but not its imports, because ``DT_NEEDED``
requires walking program headers and nothing here could test that against a real
object. Mach-O is not parsed at all. Every one of those gaps is reported as
"not checked" rather than passed, so an unverified platform never reads as a
verified one.
"""

from __future__ import annotations

import platform as platform_module
import struct
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

from tgassist.domain.model.tdlib import Architecture, BinaryFormat

_PE_SIGNATURE: Final = b"PE\x00\x00"
_ELF_MAGIC: Final = b"\x7fELF"
_MZ_MAGIC: Final = b"MZ"

_PE_MACHINE: Final = {
    0x014C: "x86",
    0x8664: "amd64",
    0xAA64: "arm64",
    0x01C4: "arm",
}
_ELF_MACHINE: Final = {
    0x03: "x86",
    0x3E: "amd64",
    0xB7: "arm64",
    0x28: "arm",
}

_IMPORT_DIRECTORY_INDEX: Final = 1
_SECTION_HEADER_SIZE: Final = 40
_IMPORT_DESCRIPTOR_SIZE: Final = 20
_MAX_IMPORTS: Final = 256
"""A library importing more than this is not one we are looking at correctly."""

_ELF_HEADER_SIZE: Final = 20
_COFF_HEADER_SIZE: Final = 24
_DOS_POINTER_SIZE: Final = 4
_PE32_MAGIC: Final = 0x10B
_PE32_DIRECTORIES_AT: Final = 96
_PE32PLUS_DIRECTORIES_AT: Final = 112
_DIRECTORY_ENTRY_SIZE: Final = 8
_NAME_LIMIT: Final = 256
_BITS_64: Final = 2**32
_MAGIC_FIELD_SIZE: Final = 2


@dataclass(frozen=True, slots=True)
class BinaryInspection:
    """What could be read from a library's headers without loading it.

    Attributes:
        format: The container format recognised, if any.
        architecture: The machine it targets.
        imports: Names of libraries it loads at runtime, lowercased. Empty when
            ``imports_readable`` is false -- absent evidence, not evidence of
            absence.
        imports_readable: Whether imports could be read for this format. False
            means "not checked", which is distinct from "checked and none".
        detail: What happened, for a diagnostic.
    """

    format: BinaryFormat = BinaryFormat.UNKNOWN
    architecture: Architecture = Architecture.UNKNOWN
    imports: tuple[str, ...] = ()
    imports_readable: bool = False
    detail: str = ""


def inspect_binary(path: Path) -> BinaryInspection:
    """Read a library's headers.

    Never raises for a malformed file: an unreadable header is a diagnostic
    result, and a loader that crashed on a corrupt binary would be worse at its
    job than one that reports it.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(4)
            if head.startswith(_ELF_MAGIC):
                return _read_elf(handle)
            if head.startswith(_MZ_MAGIC):
                return _read_pe(handle)
    except OSError as exc:
        return BinaryInspection(detail=f"could not be read: {exc}")

    return BinaryInspection(detail="not a recognised PE or ELF binary")


def current_architecture() -> Architecture:
    """Return the architecture of the running interpreter.

    What a library has to match. Derived from the pointer size and the machine
    name rather than from ``platform.machine()`` alone, because a 32-bit
    interpreter on a 64-bit machine reports the machine, not itself -- and it is
    the interpreter a library must match.
    """
    machine = platform_module.machine().lower()
    sixty_four = sys.maxsize > _BITS_64

    if machine in {"aarch64", "arm64"}:
        return Architecture.ARM64 if sixty_four else Architecture.ARM
    if machine in {"x86_64", "amd64", "i386", "i686", "x86"}:
        return Architecture.AMD64 if sixty_four else Architecture.X86
    return Architecture.UNKNOWN


# ---------------------------------------------------------------------------
# ELF
# ---------------------------------------------------------------------------


def _read_elf(handle: BinaryIO) -> BinaryInspection:
    """Read an ELF header.

    Architecture only. ``DT_NEEDED`` lives in the dynamic section, reachable
    only by walking program headers, and nothing available here could test that
    against a real object -- so it is reported as unchecked rather than guessed.
    """
    handle.seek(0)
    header = handle.read(_ELF_HEADER_SIZE)
    if len(header) < _ELF_HEADER_SIZE:
        return BinaryInspection(format=BinaryFormat.ELF, detail="truncated ELF header")

    little_endian = header[5] == 1
    order = "<" if little_endian else ">"
    (machine,) = struct.unpack_from(f"{order}H", header, 18)

    return BinaryInspection(
        format=BinaryFormat.ELF,
        architecture=Architecture(_ELF_MACHINE.get(machine, "unknown")),
        imports=(),
        imports_readable=False,
        detail="ELF: imports are not read on this platform",
    )


# ---------------------------------------------------------------------------
# PE
# ---------------------------------------------------------------------------


def _read_pe(handle: BinaryIO) -> BinaryInspection:
    """Read a PE header, its architecture and its import table."""
    read = handle.read
    seek = handle.seek

    seek(0x3C)
    raw = read(_DOS_POINTER_SIZE)
    if len(raw) < _DOS_POINTER_SIZE:
        return BinaryInspection(format=BinaryFormat.PE, detail="truncated DOS header")
    (pe_offset,) = struct.unpack("<I", raw)

    seek(pe_offset)
    coff = read(_COFF_HEADER_SIZE)
    if len(coff) < _COFF_HEADER_SIZE or coff[:4] != _PE_SIGNATURE:
        return BinaryInspection(format=BinaryFormat.PE, detail="no PE signature")

    machine, sections, _, _, _, optional_size, _ = struct.unpack_from("<HHIIIHH", coff, 4)
    architecture = Architecture(_PE_MACHINE.get(machine, "unknown"))

    optional = read(optional_size)
    if len(optional) < _MAGIC_FIELD_SIZE:
        return BinaryInspection(
            format=BinaryFormat.PE,
            architecture=architecture,
            detail="truncated optional header",
        )

    (magic,) = struct.unpack_from("<H", optional, 0)
    # PE32 keeps the directory count at 92; PE32+ has eight more bytes of
    # 64-bit fields before it.
    directories_at = _PE32_DIRECTORIES_AT if magic == _PE32_MAGIC else _PE32PLUS_DIRECTORIES_AT
    if len(optional) < directories_at + _DIRECTORY_ENTRY_SIZE * (_IMPORT_DIRECTORY_INDEX + 1):
        return BinaryInspection(
            format=BinaryFormat.PE,
            architecture=architecture,
            detail="no import directory",
        )

    import_rva, _ = struct.unpack_from(
        "<II", optional, directories_at + _DIRECTORY_ENTRY_SIZE * _IMPORT_DIRECTORY_INDEX
    )
    if import_rva == 0:
        return BinaryInspection(
            format=BinaryFormat.PE,
            architecture=architecture,
            imports=(),
            imports_readable=True,
            detail="PE: imports nothing",
        )

    table = _read_section_table(read, sections)
    imports = _read_imports(read, seek, table, import_rva)

    return BinaryInspection(
        format=BinaryFormat.PE,
        architecture=architecture,
        imports=imports,
        imports_readable=True,
        detail=f"PE: {len(imports)} imported librar{'y' if len(imports) == 1 else 'ies'}",
    )


def _read_section_table(
    read: Callable[[int], bytes], count: int
) -> tuple[tuple[int, int, int], ...]:
    """Return ``(virtual_address, virtual_size, raw_pointer)`` for each section."""
    sections: list[tuple[int, int, int]] = []
    for _ in range(count):
        raw = read(_SECTION_HEADER_SIZE)
        if len(raw) < _SECTION_HEADER_SIZE:
            break
        virtual_size, virtual_address, raw_size, raw_pointer = struct.unpack_from("<IIII", raw, 8)
        sections.append((virtual_address, max(virtual_size, raw_size), raw_pointer))
    return tuple(sections)


def _to_offset(table: tuple[tuple[int, int, int], ...], rva: int) -> int | None:
    """Translate a relative virtual address into a file offset."""
    for virtual_address, size, raw_pointer in table:
        if virtual_address <= rva < virtual_address + size:
            return raw_pointer + (rva - virtual_address)
    return None


def _read_imports(
    read: Callable[[int], bytes],
    seek: Callable[[int], int],
    table: tuple[tuple[int, int, int], ...],
    import_rva: int,
) -> tuple[str, ...]:
    """Walk the import descriptors and return the library names."""
    start = _to_offset(table, import_rva)
    if start is None:
        return ()

    names: list[str] = []
    for index in range(_MAX_IMPORTS):
        seek(start + index * _IMPORT_DESCRIPTOR_SIZE)
        descriptor = read(_IMPORT_DESCRIPTOR_SIZE)
        if len(descriptor) < _IMPORT_DESCRIPTOR_SIZE or descriptor == bytes(
            _IMPORT_DESCRIPTOR_SIZE
        ):
            break
        (name_rva,) = struct.unpack_from("<I", descriptor, 12)
        offset = _to_offset(table, name_rva)
        if offset is None:
            continue
        seek(offset)
        raw = read(_NAME_LIMIT)
        name, _, _ = raw.partition(b"\x00")
        if name:
            names.append(name.decode("ascii", errors="replace").lower())

    return tuple(names)


__all__ = [
    "BinaryInspection",
    "current_architecture",
    "inspect_binary",
]
