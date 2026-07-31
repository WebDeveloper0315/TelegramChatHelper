"""Prompts: versioned assets, rendered with untrusted content held apart.

A prompt is an *interface* between this application and a model, and it is
versioned for the same reason any interface is: when the output changes, the
question "did the model change or did we?" has to be answerable afterwards,
about data that was already being collected when it happened (ADR-057).

**No prompt text appears in a Python source file** (ADR-008). A prompt in source
is a prompt that cannot be diffed as prose, cannot be edited without a code
review, and -- worst -- has no version of its own, so the only thing recorded
against an output is the application's version. That answers "which release"
when the question is "which wording".

This module is the *model* of a prompt and the pure rule for rendering one.
Loading them from disk is infrastructure
(``infrastructure/prompts/registry.py``), because reading files is.

Untrusted content
-----------------

Conversation text is written by somebody who may be adversarial, and a prompt
that concatenates it into its instructions has handed that person the
instructions. So content never lands in the template body directly. It goes
into a **delimited slot**, and before it does, any sequence that could forge a
delimiter is neutralised -- so no message can close the slot early and continue
as though it were the prompt (``SECURITY.md`` section 12, ``PROMPTS.md``
section 6).

That is a mitigation, not a solution. No known technique makes a model reliably
immune to injection. The architectural answer is the one the rest of this
milestone implements: a successful injection reaches nothing valuable, because
the output is schema-validated, becomes a *proposal* rather than a memory, and
is shown to a person before it counts (ADR-019, ADR-058).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from tgassist.domain.errors import DomainValidationError
from tgassist.domain.model.ai import PromptVersion

#: How a template names a variable: ``{{name}}``. Deliberately not a general
#: template language -- there is no expression to evaluate, no loop to unroll
#: and no filter to apply, so there is nothing a prompt file can do except say
#: where a value goes.
PLACEHOLDER: Final = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")

#: The markers around untrusted content. The system prompt tells the model that
#: everything between them is data to analyse and never instructions to follow.
UNTRUSTED_OPEN: Final = "<<<CONVERSATION_CONTENT>>>"
UNTRUSTED_CLOSE: Final = "<<<END_CONVERSATION_CONTENT>>>"

#: Any run of three or more angle brackets is collapsed to two before untrusted
#: text is inserted. Both delimiters begin with three, so after this no quoted
#: message can produce either -- which is what stops content from closing its
#: own slot.
_DELIMITER_LIKE: Final = re.compile(r"(<{3,}|>{3,})")


def neutralise(content: str) -> str:
    """Return untrusted text that cannot forge a slot delimiter.

    Args:
        content: Text written by somebody other than the operator.

    Returns:
        The same text with every run of three or more angle brackets shortened
        to two. Chosen over stripping or escaping because it is *visible* --
        a reader of the prompt sees what the model saw -- and because it cannot
        lengthen the text, so it can never push a payload past a budget that was
        checked before it ran.
    """
    return _DELIMITER_LIKE.sub(lambda match: match.group(0)[0] * 2, content)


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    """One prompt, with its variables filled in.

    Attributes:
        text: What to send.
        version: Which prompt at which revision produced it. Carried alongside
            the text so that whatever records the call cannot record the wrong
            version -- there is nothing else to pass.
        schema_id: The output contract this prompt's answer is validated
            against, or ``None`` for a prompt that produces prose (the system
            prompt).
    """

    text: str
    version: PromptVersion
    schema_id: str | None


@dataclass(frozen=True, slots=True)
class Prompt:
    """A versioned prompt template.

    Immutable once loaded, which is the point rather than a convenience: a
    prompt that could be edited in memory would make the version recorded
    against an output a claim about a file rather than about the text that was
    actually sent.

    Attributes:
        id: What the registry knows it as, and what is recorded on every call
            made with it.
        version: Semantic version from the file's front matter. A change that
            alters the shape or meaning of the output is a major bump.
        purpose: One line, for ``tgassist`` diagnostics and for whoever opens
            the file next.
        inputs: Every variable the template requires, declared in front matter.
            Rendering validates against this list rather than against whatever
            the body happens to contain, so a template and its declaration
            cannot drift apart unnoticed.
        untrusted: Which of those inputs carry third-party text. Each is wrapped
            in delimiters and neutralised before insertion.
        schema_id: The output schema this prompt is bound to, or ``None``.
        body: The template.
    """

    id: str
    version: str
    purpose: str
    inputs: tuple[str, ...]
    untrusted: tuple[str, ...]
    schema_id: str | None
    body: str

    def __post_init__(self) -> None:
        """Validate that the prompt is coherent with itself.

        Raises:
            DomainValidationError: If the identifier or version is missing, if
                an untrusted input is not a declared input, or if the body and
                the declared inputs disagree.
        """
        if not self.id.strip():
            msg = "A prompt must have an id"
            raise DomainValidationError(msg, user_message="That prompt has no id.")
        if not self.version.strip():
            msg = f"Prompt {self.id!r} must declare a version"
            raise DomainValidationError(msg, user_message="That prompt has no version.")

        undeclared = set(self.untrusted) - set(self.inputs)
        if undeclared:
            msg = (
                f"Prompt {self.id!r} marks {sorted(undeclared)} as untrusted, "
                f"but does not declare them as inputs"
            )
            raise DomainValidationError(msg, user_message="That prompt is inconsistent.")

        # The declaration and the template must agree in *both* directions. A
        # declared input the body never uses is a caller preparing data nobody
        # reads; a placeholder nobody declared is a hole that renders as the
        # literal text ``{{name}}`` and is discovered by reading the model's
        # confused answer.
        used = set(self.placeholders())
        declared = set(self.inputs)
        if used != declared:
            msg = (
                f"Prompt {self.id!r} declares inputs {sorted(declared)} "
                f"but its body uses {sorted(used)}"
            )
            raise DomainValidationError(
                msg, user_message="That prompt's inputs do not match its text."
            )

    @property
    def version_ref(self) -> PromptVersion:
        """Return the identifier recorded against every call made with this."""
        return PromptVersion(prompt_id=self.id, version=self.version)

    def placeholders(self) -> tuple[str, ...]:
        """Return every variable the body refers to, in order of first use."""
        seen: dict[str, None] = {}
        for match in PLACEHOLDER.finditer(self.body):
            seen.setdefault(match.group(1), None)
        return tuple(seen)

    def render(self, variables: Mapping[str, str]) -> RenderedPrompt:
        """Fill this prompt in.

        Args:
            variables: A value for every declared input.

        Returns:
            The text to send, with its version and schema.

        Raises:
            DomainValidationError: If a declared input is missing, or a variable
                was supplied that the prompt does not declare. **Never
                substitutes an empty string**: a prompt silently missing its
                context section produces fluent, confident, ungrounded output,
                which is the worst failure mode available (``PROMPTS.md``
                section 5).
        """
        missing = sorted(set(self.inputs) - set(variables))
        if missing:
            msg = f"Prompt {self.id!r} requires {missing}, which was not supplied"
            raise DomainValidationError(
                msg,
                user_message="A prompt could not be prepared because information was missing.",
                context={"prompt_id": self.id, "missing": missing},
            )
        unexpected = sorted(set(variables) - set(self.inputs))
        if unexpected:
            msg = f"Prompt {self.id!r} was given {unexpected}, which it does not declare"
            raise DomainValidationError(
                msg,
                user_message="A prompt was given information it does not use.",
                context={"prompt_id": self.id, "unexpected": unexpected},
            )

        untrusted = set(self.untrusted)
        filled = PLACEHOLDER.sub(
            lambda match: _slot(match.group(1), variables[match.group(1)], untrusted),
            self.body,
        )
        return RenderedPrompt(text=filled, version=self.version_ref, schema_id=self.schema_id)


def _slot(name: str, value: str, untrusted: set[str]) -> str:
    """Return one variable's rendered form.

    Trusted values go in as they are. Untrusted ones are neutralised and wrapped
    in delimiters *here* rather than in the template, so that a prompt file
    cannot forget the wrapper and a reviewer does not have to check that it
    remembered.
    """
    if name not in untrusted:
        return value
    return f"{UNTRUSTED_OPEN}\n{neutralise(value)}\n{UNTRUSTED_CLOSE}"


__all__ = [
    "PLACEHOLDER",
    "UNTRUSTED_CLOSE",
    "UNTRUSTED_OPEN",
    "Prompt",
    "RenderedPrompt",
    "neutralise",
]
