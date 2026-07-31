"""Loading prompts from files, and refusing to start when they are wrong.

The adapter behind :class:`~tgassist.domain.ports.prompt_registry.PromptRegistry`.
It reads ``_registry.yaml``, the prompt files it names and the schemas those
declare, checks all of it, and then holds it immutably for the life of the
process.

Everything is validated at load
-------------------------------

By the time :meth:`FilePromptRegistry.get` can return a prompt, that prompt is
known to: exist; carry front matter that parses; declare an id matching its
registry key and a version; use exactly the variables it declares; and name a
schema that exists, parses, and uses only keywords the validator implements.

The alternative -- checking when a prompt is used -- fails at the worst
possible moment, on the machine of somebody who cannot fix it, after a model
call has already been paid for (ADR-026 section 7).

Immutable once loaded
---------------------

Prompts are read once, into frozen dataclasses behind a read-only mapping.
There is no reload and no setter. The version recorded against a model call has
to be a claim about the text that was actually sent, and a registry that could
be edited in memory would make it a claim about a file that may since have
changed.

Where the files live
--------------------

Inside the package (``tgassist/prompts``), not beside it. A prompt is an asset
the application cannot run without, and one that sat outside the wheel would be
missing from every installation that was not a git checkout. ``PROMPTS.md``
section 2 originally put them at the repository root; this is the same tree in
a place that ships.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import yaml

from tgassist.domain.errors import (
    DomainValidationError,
    PromptNotFoundError,
    PromptRegistryInvalidError,
)
from tgassist.domain.model.prompt import Prompt
from tgassist.domain.ports.prompt_registry import PromptRegistry
from tgassist.domain.services.structured_output import JsonSchema, require_supported

#: The index file, relative to the prompt directory.
REGISTRY_FILE: Final = "_registry.yaml"

#: The registry format this loader understands. Bumped if the file's shape
#: changes; checked so that an older application reading a newer file says so
#: instead of quietly missing half of it.
REGISTRY_VERSION: Final = 1

#: Where the shipped prompts live.
DEFAULT_PROMPT_DIR: Final = Path(__file__).resolve().parent.parent.parent / "prompts"

_FRONT_MATTER_FENCE: Final = "---"

#: A prompt file splits into three around its fences: the empty text before the
#: opening one, the front matter, and the body.
_FRONT_MATTER_PARTS: Final = 3


class FilePromptRegistry(PromptRegistry):
    """The prompts this application ships, read from files.

    Attributes:
        directory: Where the prompts were read from. Reported by diagnostics,
            because "which prompts is it using" is otherwise unanswerable from
            outside.
    """

    __slots__ = ("_prompts", "_schemas", "directory")

    def __init__(self, directory: Path | None = None) -> None:
        """Prepare to load from a directory.

        Nothing is read here. Construction happens at composition time, and a
        constructor that touched the filesystem would make building the object
        graph a thing that can fail for a reason unrelated to the graph.

        Args:
            directory: Where to read from. Defaults to the shipped prompts.
        """
        self.directory = directory if directory is not None else DEFAULT_PROMPT_DIR
        self._prompts: dict[str, Prompt] = {}
        self._schemas: dict[str, JsonSchema | None] = {}

    @property
    def is_loaded(self) -> bool:
        """Whether the registry has read its files."""
        return bool(self._prompts)

    def load(self) -> None:
        """Read and validate every prompt.

        Idempotent: loading a registry that is already loaded does nothing, so
        the startup path can call it without checking and a second container in
        the same process cannot double the work.

        Raises:
            PromptRegistryInvalidError: If the index is missing or malformed, if
                a prompt file is missing or inconsistent with its declaration,
                or if a schema is missing, unparseable or uses an unsupported
                keyword.
        """
        if self._prompts:
            return

        index = self._read_index()
        prompts: dict[str, Prompt] = {}
        schemas: dict[str, JsonSchema | None] = {}

        for prompt_id, entry in index.items():
            prompt = self._read_prompt(prompt_id, entry)
            prompts[prompt_id] = prompt
            schemas[prompt_id] = self._read_schema(prompt_id, entry.get("schema"))

        self._prompts = prompts
        self._schemas = schemas

    # -- The port ----------------------------------------------------------

    def get(self, prompt_id: str) -> Prompt:
        """Return one prompt.

        Raises:
            PromptNotFoundError: If the registry holds no prompt of that name,
                or has not been loaded. Both are the same mistake from the
                caller's side -- the prompt is not available -- and the message
                says which.
        """
        self._require_loaded(prompt_id)
        found = self._prompts.get(prompt_id)
        if found is None:
            msg = f"No prompt {prompt_id!r} in {self.directory}"
            raise PromptNotFoundError(
                msg,
                user_message="A prompt this feature needs is not installed.",
                context={"prompt_id": prompt_id, "known": sorted(self._prompts)},
            )
        return found

    def schema_for(self, prompt_id: str) -> JsonSchema | None:
        """Return the output contract bound to a prompt, if it has one.

        Raises:
            PromptNotFoundError: If the registry holds no prompt of that name.
        """
        self.get(prompt_id)
        return self._schemas.get(prompt_id)

    # -- Reading -----------------------------------------------------------

    def _read_index(self) -> dict[str, dict[str, Any]]:
        """Read ``_registry.yaml`` and return its prompt entries."""
        path = self.directory / REGISTRY_FILE
        raw = self._read_text(path, what="prompt registry")

        try:
            document = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            msg = f"The prompt registry at {path} is not valid YAML"
            raise PromptRegistryInvalidError(
                msg, user_message="The prompt registry is unreadable.", cause=exc
            ) from exc

        if not isinstance(document, dict):
            self._invalid(f"The prompt registry at {path} is not a mapping")

        version = document.get("version")
        if version != REGISTRY_VERSION:
            self._invalid(
                f"The prompt registry at {path} declares version {version!r}; "
                f"this application understands {REGISTRY_VERSION}"
            )

        entries = document.get("prompts")
        if not isinstance(entries, dict) or not entries:
            self._invalid(f"The prompt registry at {path} lists no prompts")

        for prompt_id, entry in entries.items():
            if not isinstance(entry, dict) or "path" not in entry:
                self._invalid(f"Registry entry {prompt_id!r} has no path")
        return entries

    def _read_prompt(self, prompt_id: str, entry: dict[str, Any]) -> Prompt:
        """Read one prompt file and check it against its registry entry."""
        path = self.directory / str(entry["path"])
        raw = self._read_text(path, what=f"prompt {prompt_id!r}")
        front_matter, body = self._split(raw, path)

        declared_id = front_matter.get("id")
        if declared_id != prompt_id:
            self._invalid(
                f"Prompt file {path} declares id {declared_id!r}, "
                f"but the registry lists it as {prompt_id!r}"
            )

        schema_id = entry.get("schema")
        declared_schema = front_matter.get("output_schema")
        if declared_schema != schema_id:
            # Both places name the schema, so both places can disagree. The
            # version deliberately lives in only one place; this one is
            # duplicated because the prompt's own text refers to its output
            # shape, and a reader of the file should see which contract that is.
            self._invalid(
                f"Prompt {prompt_id!r} declares output_schema {declared_schema!r}, "
                f"but the registry binds it to {schema_id!r}"
            )

        try:
            return Prompt(
                id=prompt_id,
                version=str(front_matter.get("version", "")),
                purpose=str(front_matter.get("purpose", "")),
                inputs=_as_tuple(front_matter.get("inputs")),
                untrusted=_as_tuple(front_matter.get("untrusted")),
                schema_id=str(schema_id) if schema_id else None,
                body=body,
            )
        except DomainValidationError as exc:
            msg = f"Prompt {prompt_id!r} at {path} is not usable: {exc}"
            raise PromptRegistryInvalidError(
                msg,
                user_message="A prompt this application ships is inconsistent.",
                context={"prompt_id": prompt_id},
                cause=exc,
            ) from exc

    def _read_schema(self, prompt_id: str, schema_path: Any) -> JsonSchema | None:
        """Read and check the schema a prompt is bound to, if it has one."""
        if not schema_path:
            return None

        path = self.directory / str(schema_path)
        raw = self._read_text(path, what=f"schema for prompt {prompt_id!r}")
        try:
            definition = json.loads(raw)
        except json.JSONDecodeError as exc:
            msg = f"The schema at {path} is not valid JSON"
            raise PromptRegistryInvalidError(
                msg, user_message="A prompt schema is unreadable.", cause=exc
            ) from exc

        if not isinstance(definition, dict):
            self._invalid(f"The schema at {path} is not a JSON object")

        try:
            require_supported(definition, schema_id=str(schema_path))
        except DomainValidationError as exc:
            raise PromptRegistryInvalidError(
                str(exc),
                user_message="A prompt schema uses something this application cannot check.",
                context={"prompt_id": prompt_id, "schema": str(schema_path)},
                cause=exc,
            ) from exc

        return JsonSchema(id=str(schema_path), definition=MappingProxyType(definition))

    # -- Parsing -----------------------------------------------------------

    def _split(self, raw: str, path: Path) -> tuple[dict[str, Any], str]:
        """Separate YAML front matter from the template body."""
        if not raw.lstrip().startswith(_FRONT_MATTER_FENCE):
            self._invalid(f"Prompt file {path} has no front matter")

        parts = raw.lstrip().split(_FRONT_MATTER_FENCE, 2)
        if len(parts) < _FRONT_MATTER_PARTS:
            self._invalid(f"Prompt file {path} has unterminated front matter")

        try:
            front_matter = yaml.safe_load(parts[1])
        except yaml.YAMLError as exc:
            msg = f"The front matter of {path} is not valid YAML"
            raise PromptRegistryInvalidError(
                msg, user_message="A prompt file is unreadable.", cause=exc
            ) from exc

        if not isinstance(front_matter, dict):
            self._invalid(f"The front matter of {path} is not a mapping")
        return front_matter, parts[2].strip()

    def _read_text(self, path: Path, *, what: str) -> str:
        """Read a file, or say which one is missing."""
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            msg = f"The {what} is missing: {path}"
            raise PromptRegistryInvalidError(
                msg,
                user_message="A prompt file this application needs is missing.",
                context={"path": str(path)},
                cause=exc,
            ) from exc
        except OSError as exc:
            msg = f"The {what} at {path} could not be read"
            raise PromptRegistryInvalidError(
                msg,
                user_message="A prompt file could not be read.",
                context={"path": str(path)},
                cause=exc,
            ) from exc

    def _require_loaded(self, prompt_id: str) -> None:
        """Refuse to answer before the files have been read."""
        if not self._prompts:
            msg = f"The prompt registry has not been loaded; asked for {prompt_id!r}"
            raise PromptNotFoundError(
                msg,
                user_message="Prompts are not loaded.",
                context={"directory": str(self.directory)},
            )

    def _invalid(self, message: str) -> None:
        """Raise the one error this loader reports."""
        raise PromptRegistryInvalidError(message, user_message="The prompt registry is not valid.")


def _as_tuple(value: Any) -> tuple[str, ...]:
    """Return a front-matter list as a tuple of names."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


__all__ = ["DEFAULT_PROMPT_DIR", "REGISTRY_FILE", "REGISTRY_VERSION", "FilePromptRegistry"]
