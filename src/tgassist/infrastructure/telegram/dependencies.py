"""Which runtime dependencies a trusted ``tdjson`` may have.

The manifest checksums one file. Whatever that file loads at runtime is inside
the trust boundary and is **not** checked, so a `tdjson` importing OpenSSL is
covered by its digest only in part (ADR-047).

The gap is invisible rather than noisy. CPython resolves a library path in full
and adds its directory to the search order, so a dynamically linked ``tdjson``
with ``libcrypto`` beside it loads and works perfectly -- while three unverified
files sit inside the boundary. Nothing fails. That is exactly why this check
exists: the failure mode is silence.

Three classes of import:

* **System** -- shipped with the operating system, present on every machine,
  outside anything we could meaningfully verify. Accepted.
* **Forbidden** -- the cryptography and compression libraries TDLib links.
  Their presence means the artefact is not self-contained and the digest covers
  less than it appears to. Rejected.
* **Unrecognised** -- anything else. Rejected, because "unexpected third-party
  runtime dependency" is the case this exists to catch, and an allow-list that
  silently admits the unknown is not an allow-list.
"""

from __future__ import annotations

from typing import Final

from tgassist.domain.model.tdlib import DependencyReport, DependencyVerdict

WINDOWS_SYSTEM_LIBRARIES: Final[frozenset[str]] = frozenset(
    {
        "advapi32.dll",
        "bcrypt.dll",
        "comdlg32.dll",
        "crypt32.dll",
        "dbghelp.dll",
        "dnsapi.dll",
        "gdi32.dll",
        "iphlpapi.dll",
        "kernel32.dll",
        "mswsock.dll",
        "ncrypt.dll",
        "netapi32.dll",
        "normaliz.dll",
        "ole32.dll",
        "oleaut32.dll",
        "psapi.dll",
        "rpcrt4.dll",
        "secur32.dll",
        "shell32.dll",
        "shlwapi.dll",
        "user32.dll",
        "userenv.dll",
        "version.dll",
        "winmm.dll",
        "wldap32.dll",
        "ws2_32.dll",
    }
)
"""Windows libraries that are part of the operating system.

Present on every supported Windows installation, so importing one adds nothing
to the trust boundary that was not already there.
"""

SYSTEM_PREFIXES: Final[tuple[str, ...]] = ("api-ms-win-", "ext-ms-win-")
"""API-set stubs. Not files on disk; the loader resolves them to system code."""

REDISTRIBUTABLE_LIBRARIES: Final[frozenset[str]] = frozenset(
    {"vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll", "ucrtbase.dll", "msvcrt.dll"}
)
"""The Visual C++ runtime.

Accepted, but reported: their presence means the build did **not** use the
static C runtime, so the target machine needs the redistributable installed.
That is a deployment fact worth surfacing rather than a security problem.
"""

FORBIDDEN_FRAGMENTS: Final[tuple[str, ...]] = (
    "libcrypto",
    "libssl",
    "ssleay",
    "libeay",
    "openssl",
    "zlib",
    "zlib1",
    "libz",
)
"""Cryptography and compression, matched as fragments so a version suffix does
not evade the check: ``libcrypto-3-x64.dll`` and ``libcrypto.so.3`` both match."""


def classify_dependencies(imports: tuple[str, ...], *, readable: bool) -> DependencyReport:
    """Classify a library's imports.

    Args:
        imports: Imported library names, lowercased.
        readable: Whether imports could be read at all. ``False`` yields
            ``NOT_CHECKED`` regardless of ``imports``, because an empty list
            from a format we cannot parse is absent evidence rather than
            evidence of absence.
    """
    if not readable:
        return DependencyReport(
            verdict=DependencyVerdict.NOT_CHECKED,
            detail="imports cannot be read for this binary format on this platform",
        )

    system: list[str] = []
    redistributable: list[str] = []
    forbidden: list[str] = []
    unrecognised: list[str] = []

    for name in imports:
        lowered = name.lower()
        if any(fragment in lowered for fragment in FORBIDDEN_FRAGMENTS):
            forbidden.append(name)
        elif lowered in WINDOWS_SYSTEM_LIBRARIES or lowered.startswith(SYSTEM_PREFIXES):
            system.append(name)
        elif lowered in REDISTRIBUTABLE_LIBRARIES:
            redistributable.append(name)
        else:
            unrecognised.append(name)

    if forbidden:
        return DependencyReport(
            verdict=DependencyVerdict.FORBIDDEN,
            system=tuple(system),
            redistributable=tuple(redistributable),
            forbidden=tuple(forbidden),
            unrecognised=tuple(unrecognised),
            detail=(f"loads {', '.join(forbidden)} at runtime, which the manifest does not verify"),
        )

    if unrecognised:
        return DependencyReport(
            verdict=DependencyVerdict.UNRECOGNISED,
            system=tuple(system),
            redistributable=tuple(redistributable),
            unrecognised=tuple(unrecognised),
            detail=f"imports {', '.join(unrecognised)}, which this check cannot vouch for",
        )

    noted = (
        f"; needs the Visual C++ redistributable ({', '.join(redistributable)})"
        if redistributable
        else ""
    )
    return DependencyReport(
        verdict=DependencyVerdict.ACCEPTABLE,
        system=tuple(system),
        redistributable=tuple(redistributable),
        detail=f"{len(system)} system librar{'y' if len(system) == 1 else 'ies'}{noted}",
    )


__all__ = [
    "FORBIDDEN_FRAGMENTS",
    "REDISTRIBUTABLE_LIBRARIES",
    "WINDOWS_SYSTEM_LIBRARIES",
    "classify_dependencies",
]
