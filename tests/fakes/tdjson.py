"""Fake native libraries, and the openers that produce them.

Loading real native code is the one thing in this project that cannot be tested
directly: a test would need a C compiler present, and the suite would stop being
deterministic on a machine that lacks one. These doubles are the second
implementation of ``NativeLibrary`` that justifies the protocol existing.

Each double models one way a real library goes wrong, and every one of them has
been seen in practice: a file that is not a library at all, an old TDLib whose
symbols differ, a library that loads but answers nothing, a platform that
refuses to open a file it can see.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Callable, Sequence
from pathlib import Path

from tgassist.infrastructure.telegram.loader import REQUIRED_SYMBOLS, NativeLibrary

TDLIB_VERSION = "1.8.29"
"""What a healthy fake reports. A real version, so the comparison is realistic."""


class FakeTdjson:
    """A library that behaves like a working TDLib.

    Records the requests it was given, so a test can assert that the loader
    silenced TDLib's logging before doing anything else.
    """

    __slots__ = ("_symbols", "_version", "requests")

    def __init__(
        self,
        *,
        version: str | None = TDLIB_VERSION,
        symbols: tuple[str, ...] = REQUIRED_SYMBOLS,
    ) -> None:
        """Build a library exporting ``symbols`` and reporting ``version``."""
        self._version = version
        self._symbols = symbols
        self.requests: list[dict[str, object]] = []

    def has_symbol(self, name: str) -> bool:
        """Report whether this library exports an entry point."""
        return name in self._symbols

    def execute(self, request: str) -> str | None:
        """Answer a synchronous request, as ``td_execute`` does."""
        document = json.loads(request)
        self.requests.append(document)

        if document.get("@type") == "setLogVerbosityLevel":
            return json.dumps({"@type": "ok"})
        if document.get("@type") == "getOption" and document.get("name") == "version":
            if self._version is None:
                return None
            return json.dumps({"@type": "optionValueString", "value": self._version})
        return None


class SilentLibrary:
    """A library that exports everything but answers no request.

    A file with the right symbol names that is not TDLib -- a stub, a wrapper,
    or a build with its query interface compiled out.
    """

    __slots__ = ()

    def has_symbol(self, name: str) -> bool:
        """Report every required entry point as present."""
        return name in REQUIRED_SYMBOLS

    def execute(self, request: str) -> str | None:
        """Answer nothing, whatever is asked."""
        return None


class HostileLibrary:
    """A library whose ``td_execute`` raises.

    Real ``ctypes`` calls into a wrong library fail in ways Python cannot
    predict -- ``OSError``, ``ValueError``, an access violation. The loader must
    treat any of them as "this is not usable" rather than propagating.
    """

    __slots__ = ()

    def has_symbol(self, name: str) -> bool:
        """Report every required entry point as present."""
        return name in REQUIRED_SYMBOLS

    def execute(self, request: str) -> str | None:
        """Fail, as a mismatched binary would."""
        msg = f"exception from a mismatched library for {request[:20]}"
        raise OSError(msg)


class MalformedReplyLibrary:
    """A library returning something that is not JSON."""

    __slots__ = ()

    def has_symbol(self, name: str) -> bool:
        """Report every required entry point as present."""
        return name in REQUIRED_SYMBOLS

    def execute(self, request: str) -> str | None:
        """Return bytes that do not parse."""
        return "not json at all"


def opener_for(library: NativeLibrary) -> Callable[[Path], NativeLibrary]:
    """Return an opener that always yields one library."""

    def _open(path: Path) -> NativeLibrary:  # noqa: ARG001 - the path is irrelevant
        return library

    return _open


def refusing_opener(
    message: str = "cannot open shared object file",
) -> Callable[[Path], NativeLibrary]:
    """Return an opener that fails as a platform does.

    The commonest real failure: the file is present and readable, but a
    transitive dependency is missing or the architecture is wrong.
    """

    def _open(path: Path) -> NativeLibrary:
        raise OSError(f"{path}: {message}")

    return _open


def write_library(path: Path, content: bytes = b"not a real library") -> Path:
    """Write a file standing in for a shared library.

    Its bytes are what the digest is computed over, so the content only has to
    be stable and distinguishable -- the loader never interprets it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


__all__ = [
    "TDLIB_VERSION",
    "FakeTdjson",
    "HostileLibrary",
    "MalformedReplyLibrary",
    "SilentLibrary",
    "make_elf",
    "make_pe",
    "opener_for",
    "refusing_opener",
    "write_library",
]


# ---------------------------------------------------------------------------
# Synthetic binaries
# ---------------------------------------------------------------------------

PE_MACHINE = {"amd64": 0x8664, "x86": 0x014C, "arm64": 0xAA64}
ELF_MACHINE = {"amd64": 0x3E, "x86": 0x03, "arm64": 0xB7}

_PE_OFFSET = 0x80
_OPTIONAL_SIZE = 240
_SECTION_RVA = 0x1000
_SECTION_RAW = 0x200
_DESCRIPTOR = 20


def make_pe(machine: str = "amd64", imports: Sequence[str] = ()) -> bytes:
    """Build a synthetic PE library with a given architecture and import table.

    Enough of the format for the header reader to walk: DOS stub, PE signature,
    COFF and optional headers, one section, and a real import directory. Written
    by hand rather than checked in as a binary fixture so a reader can see
    exactly what is being parsed, and so a new case is a function call rather
    than a new file.
    """
    names_rva = _SECTION_RVA + (len(imports) + 1) * _DESCRIPTOR

    descriptors = bytearray()
    strings = bytearray()
    for name in imports:
        encoded = name.encode("ascii") + b"\x00"
        descriptors += struct.pack("<IIIII", 0, 0, 0, names_rva + len(strings), 0)
        strings += encoded
    descriptors += bytes(_DESCRIPTOR)  # null terminator

    section_data = bytes(descriptors) + bytes(strings)

    coff = struct.pack(
        "<4sHHIIIHH",
        b"PE\x00\x00",
        PE_MACHINE[machine],
        1,  # one section
        0,
        0,
        0,
        _OPTIONAL_SIZE,
        0x2022,  # DLL, executable
    )

    optional = bytearray(_OPTIONAL_SIZE)
    struct.pack_into("<H", optional, 0, 0x20B)  # PE32+
    # Data directory 1 is the import table; PE32+ keeps directories at 112.
    struct.pack_into("<II", optional, 112 + 8, _SECTION_RVA, len(section_data))

    section = struct.pack(
        "<8sIIII12x",
        b".idata\x00\x00",
        len(section_data),
        _SECTION_RVA,
        len(section_data),
        _SECTION_RAW,
    )

    image = bytearray(_SECTION_RAW + len(section_data))
    image[0:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, _PE_OFFSET)
    image[_PE_OFFSET : _PE_OFFSET + len(coff)] = coff
    optional_at = _PE_OFFSET + len(coff)
    image[optional_at : optional_at + _OPTIONAL_SIZE] = optional
    section_at = optional_at + _OPTIONAL_SIZE
    image[section_at : section_at + len(section)] = section
    image[_SECTION_RAW : _SECTION_RAW + len(section_data)] = section_data
    return bytes(image)


def make_elf(machine: str = "amd64") -> bytes:
    """Build a synthetic ELF header.

    Only the identification and machine fields, because only those are read:
    imports live in the dynamic section, which this project does not parse
    (see ``infrastructure/telegram/binary.py``).
    """
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 2  # 64-bit
    header[5] = 1  # little-endian
    header[6] = 1  # version
    struct.pack_into("<H", header, 16, 3)  # ET_DYN, a shared object
    struct.pack_into("<H", header, 18, ELF_MACHINE[machine])
    return bytes(header)
