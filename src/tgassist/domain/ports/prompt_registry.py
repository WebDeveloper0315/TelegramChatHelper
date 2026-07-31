"""Prompt registry port: where versioned prompt assets come from.

A port rather than a direct import, for the usual reason and one specific one.
The usual: prompts live in files, reading files is I/O, and the application
layer does not do I/O. The specific: a test that needed a prompt would otherwise
either read the shipped one -- coupling a test about extraction to the exact
wording of a prompt -- or invent its own loader.

**Discovery is never by convention.** A registry maps a name to a prompt; the
filesystem layout is an implementation detail of whoever implements this. A
loader that globbed a directory would silently lose a prompt on rename and
silently gain one on a stray file, and both failures surface as a model
answering the wrong question.

Two operations, each with a caller in this slice:

* :meth:`get` -- ``ExtractMemories``, for the system prompt and the extraction
  prompt.
* :meth:`schema_for` -- ``ExtractMemories``, to validate what comes back.

There is no ``list`` and no ``reload``. A listing has no caller until a
diagnostic command wants one; a reload would make "immutable once loaded" a
promise with an exception in it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tgassist.domain.model.prompt import Prompt
from tgassist.domain.services.structured_output import JsonSchema


@runtime_checkable
class PromptRegistry(Protocol):
    """The prompts this application ships, indexed by name.

    Implementations validate everything they hold **when they load**, not when
    a prompt is asked for: a broken prompt found while a user waits is the same
    defect found at the worst moment (ADR-026 section 7). By the time a caller
    can reach this interface, every prompt in it is known to parse, to declare
    the variables its body uses, and to name a schema that exists.
    """

    def get(self, prompt_id: str) -> Prompt:
        """Return one prompt.

        Raises:
            PromptNotFoundError: If the registry holds no prompt of that name.
        """
        ...

    def schema_for(self, prompt_id: str) -> JsonSchema | None:
        """Return the output contract bound to a prompt, if it has one.

        ``None`` for a prompt that produces prose rather than data -- the system
        prompt is the only one today.

        Raises:
            PromptNotFoundError: If the registry holds no prompt of that name.
        """
        ...
