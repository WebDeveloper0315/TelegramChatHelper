"""Architectural contract tests.

These run first and fastest because they encode the guarantees the whole design
rests on. `import-linter` enforces the same contracts across the package graph;
these add assertions it cannot express and give a readable failure when a rule
is broken.

A comment stating that the domain must not import infrastructure is a wish.
A test asserting it is a guarantee that survives refactoring and new
contributors.
"""

from __future__ import annotations

import ast
import importlib
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
PACKAGE = SRC / "tgassist"

DOMAIN = PACKAGE / "domain"
APPLICATION = PACKAGE / "application"
INFRASTRUCTURE = PACKAGE / "infrastructure"
PRESENTATION = PACKAGE / "presentation"

COMPOSITION_ROOT = APPLICATION / "container.py"


@dataclass(frozen=True)
class Import:
    """A single imported module name and where it was found."""

    module: str
    source: Path
    line: int

    def __str__(self) -> str:
        """Render the import as a file:line reference."""
        return f"{self.source.relative_to(SRC)}:{self.line} imports {self.module}"


def python_files(root: Path) -> list[Path]:
    """Return every Python file beneath a directory."""
    return sorted(root.rglob("*.py"))


def imports_of(path: Path) -> list[Import]:
    """Return every module imported by a file, as written."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[Import] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(Import(alias.name, path, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append(Import(node.module, path, node.lineno))
    return found


def top_level(module: str) -> str:
    """Return the first component of a dotted module name."""
    return module.partition(".")[0]


def is_standard_library(module: str) -> bool:
    """Report whether a module is part of the standard library."""
    return top_level(module) in sys.stdlib_module_names


def violations(root: Path, *, forbidden_prefixes: tuple[str, ...]) -> list[Import]:
    """Return imports under ``root`` matching any forbidden prefix."""
    return [
        imported
        for path in python_files(root)
        for imported in imports_of(path)
        if imported.module.startswith(forbidden_prefixes)
    ]


class TestDomainIndependence:
    """The domain is the centre. It depends on nothing (ADR-011)."""

    def test_domain_imports_no_other_layer(self) -> None:
        found = violations(
            DOMAIN,
            forbidden_prefixes=(
                "tgassist.application",
                "tgassist.infrastructure",
                "tgassist.presentation",
            ),
        )

        assert not found, "Domain must not depend on outer layers:\n" + "\n".join(map(str, found))

    def test_domain_imports_no_third_party_package(self) -> None:
        offenders = [
            imported
            for path in python_files(DOMAIN)
            for imported in imports_of(path)
            if not is_standard_library(imported.module) and top_level(imported.module) != "tgassist"
        ]

        assert not offenders, (
            "Domain must be free of third-party dependencies so it can be "
            "exercised without a database, a network or a model:\n" + "\n".join(map(str, offenders))
        )


class TestLayerDirection:
    """Dependencies point inward, toward the domain."""

    def test_application_does_not_import_infrastructure_outside_the_composition_root(self) -> None:
        offenders = [
            imported
            for path in python_files(APPLICATION)
            if path != COMPOSITION_ROOT
            for imported in imports_of(path)
            if imported.module.startswith("tgassist.infrastructure")
        ]

        assert not offenders, (
            "Only the composition root may construct infrastructure:\n"
            + "\n".join(map(str, offenders))
        )

    def test_composition_root_exists(self) -> None:
        # The exemption above is only sound while the exempted file is the
        # single, named composition root.
        assert COMPOSITION_ROOT.is_file()

    def test_infrastructure_does_not_import_application_or_presentation(self) -> None:
        found = violations(
            INFRASTRUCTURE,
            forbidden_prefixes=("tgassist.application", "tgassist.presentation"),
        )

        assert not found, "Infrastructure may depend only on the domain:\n" + "\n".join(
            map(str, found)
        )

    def test_presentation_does_not_import_infrastructure(self) -> None:
        found = violations(PRESENTATION, forbidden_prefixes=("tgassist.infrastructure",))

        assert not found, (
            "Presentation must reach infrastructure through the application layer:\n"
            + "\n".join(map(str, found))
        )


class TestPackageStructure:
    """The tree matches ARCHITECTURE.md section 10."""

    @pytest.mark.parametrize(
        "relative",
        [
            "domain/model",
            "domain/ports",
            "domain/services",
            "application/use_cases",
            "application/policies",
            "application/event_handlers",
            "infrastructure/telegram",
            "infrastructure/persistence",
            "infrastructure/ai",
            "infrastructure/embeddings",
            "infrastructure/config",
            "infrastructure/logging",
            "infrastructure/security",
            "infrastructure/events",
            "infrastructure/tasks",
            "infrastructure/plugins",
            "presentation/cli",
            "presentation/desktop",
        ],
    )
    def test_package_exists_and_is_documented(self, relative: str) -> None:
        init = PACKAGE / relative / "__init__.py"

        assert init.is_file(), f"Missing package: {relative}"
        assert ast.get_docstring(ast.parse(init.read_text(encoding="utf-8"))), (
            f"Package {relative} must state its responsibility in a docstring"
        )

    def test_tests_and_docs_are_not_inside_the_source_tree(self) -> None:
        assert not (PACKAGE / "tests").exists()
        assert not (PACKAGE / "docs").exists()


class TestNoCircularImports:
    def test_every_module_imports_cleanly(self) -> None:
        for path in python_files(PACKAGE):
            if path.name == "__main__.py":
                continue
            relative = path.relative_to(SRC).with_suffix("")
            parts = list(relative.parts)
            if parts[-1] == "__init__":
                parts.pop()
            importlib.import_module(".".join(parts))
