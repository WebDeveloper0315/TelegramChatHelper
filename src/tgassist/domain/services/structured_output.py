"""Validating what a model answered, before anything believes it.

A model's answer is *text*. Everything downstream wants a structure, and the
step between the two is where a hallucination, an injection or a version change
is caught -- or is not. This module is that step, and it is deliberately the
dullest code in the milestone: it checks shape, and it has no opinion about
meaning.

The division of labour
----------------------

**This validator decides whether the answer is the right shape.** Required
fields present, types right, enumerations respected, numbers in range, arrays
bounded. Nothing else.

**The use case decides whether the answer is worth acting on.** Confidence below
a threshold, evidence that does not appear in the conversation, a fact already
proposed. Those are policy, they change per feature, and a validator that knew
about them would have to change every time a feature did.

Keeping them apart is what lets the same validator serve summaries and reply
suggestions later without acquiring an argument for each.

One repair attempt
------------------

A model that answered with prose, or with JSON missing a field, is often right
about the content and wrong about the format, and telling it exactly what was
wrong usually fixes it. Once. A second repair is a different failure -- the
model has now been told twice -- and retrying it costs money to arrive at the
same place (ADR-020 section 4).

:func:`build_repair_prompt` produces the second attempt's instructions. It does
not *make* the attempt: that needs a provider, and this module is pure so that
the rule can be tested without one.

A deliberately small subset of JSON Schema
------------------------------------------

Rather than take a dependency, this implements the keywords the shipped schemas
actually use. The safety of that trade-off rests on one thing:
:func:`require_supported` rejects a schema using anything else **at load time**,
so an unimplemented keyword is a startup failure rather than a constraint that
silently passes. A hand-written validator that ignored what it did not
understand would be worse than no validator, because it would look like one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from tgassist.domain.errors import DomainValidationError

#: Every keyword this validator implements. A schema using anything else is
#: refused when it is loaded, so "unsupported" can never mean "ignored".
SUPPORTED_KEYWORDS: Final = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "title",
        "description",
        "$schema",
    }
)

#: JSON Schema type names, mapped to what Python parses them as. ``integer`` is
#: checked separately because ``bool`` is an ``int`` in Python and ``true`` is
#: not an integer in JSON.
_TYPES: Final[dict[str, tuple[type, ...]]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "null": (type(None),),
}

#: How many violations to report. Enough to fix the answer in one repair,
#: bounded so a wildly wrong payload cannot produce a repair prompt larger than
#: the original request.
MAX_VIOLATIONS: Final = 10


@dataclass(frozen=True, slots=True)
class JsonSchema:
    """One output contract, bound to a prompt.

    Attributes:
        id: What the registry knows it as -- the path in the prompt's front
            matter. Recorded in violation messages so a failure names the
            contract it failed.
        definition: The schema itself, already checked for supported keywords.
    """

    id: str
    definition: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    """What validation found.

    Attributes:
        payload: The parsed answer, or ``None`` if it could not be parsed or did
            not satisfy the schema. There is no partial result: a caller that
            could reach a half-valid payload would eventually use one.
        violations: What was wrong, in the words the repair prompt uses. Empty
            when valid.
    """

    payload: Mapping[str, Any] | None
    violations: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        """Whether the answer satisfied its schema."""
        return self.payload is not None


def require_supported(definition: Mapping[str, Any], *, schema_id: str) -> None:
    """Refuse a schema that uses a keyword this validator does not implement.

    Called when a schema is loaded, so that the guarantee "validated against its
    schema" means the whole schema.

    Args:
        definition: The schema, or a subschema during recursion.
        schema_id: What to name in the error.

    Raises:
        DomainValidationError: If any keyword is unsupported.
    """
    unsupported = sorted(set(definition) - SUPPORTED_KEYWORDS)
    if unsupported:
        msg = (
            f"Schema {schema_id!r} uses {unsupported}, which the validator does not "
            f"implement. An unimplemented keyword would be silently ignored, so it "
            f"is refused instead."
        )
        raise DomainValidationError(msg, user_message="A prompt schema is not supported.")

    properties = definition.get("properties")
    if isinstance(properties, Mapping):
        for child in properties.values():
            if isinstance(child, Mapping):
                require_supported(child, schema_id=schema_id)

    items = definition.get("items")
    if isinstance(items, Mapping):
        require_supported(items, schema_id=schema_id)


def parse(text: str) -> Any | None:
    """Return the JSON value in a model's answer, or ``None``.

    Tolerant of exactly one habit: models wrap JSON in a Markdown code fence
    even when told not to. Stripping the fence is deterministic and costs
    nothing; refusing it would spend a repair attempt on punctuation.

    Nothing else is repaired here -- no quote fixing, no trailing-comma removal,
    no extracting the first ``{`` to the last ``}``. Each of those turns "the
    model answered wrongly" into "the application guessed", and a guess that
    parses is worse than a failure that does not.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        without_open = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = without_open.rsplit("```", 1)[0].strip()
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None


def validate(text: str, schema: JsonSchema) -> ValidationOutcome:
    """Check a model's answer against its schema.

    Args:
        text: What the model said.
        schema: The contract its prompt is bound to.

    Returns:
        The parsed payload, or the violations that stopped it.
    """
    payload = parse(text)
    if payload is None:
        return ValidationOutcome(
            payload=None,
            violations=("the answer was not valid JSON; reply with JSON only",),
        )

    violations = _check(payload, schema.definition, path="")
    if violations:
        return ValidationOutcome(payload=None, violations=tuple(violations[:MAX_VIOLATIONS]))
    if not isinstance(payload, Mapping):
        # Every schema this application binds is an object at the top level, and
        # a caller that received a list here would index it by key.
        return ValidationOutcome(payload=None, violations=("the answer must be a JSON object",))
    return ValidationOutcome(payload=payload, violations=())


def build_repair_prompt(text: str, violations: Sequence[str]) -> str:
    """Build the instructions for the one repair attempt.

    The model is given back its own answer and told exactly what was wrong with
    it. Its own answer, rather than the original request, for two reasons: it is
    shorter, and the fault is in the answer.

    Args:
        text: What the model said the first time.
        violations: What was wrong with it.

    Returns:
        Content for a second call.

    Raises:
        DomainValidationError: If there is nothing to repair, which would mean a
            caller asked for a repair of a valid answer.
    """
    if not violations:
        msg = "A repair needs something to repair"
        raise DomainValidationError(msg, user_message="There is nothing to correct.")

    problems = "\n".join(f"- {violation}" for violation in violations)
    return (
        "Your previous answer did not satisfy the required format.\n\n"
        f"Problems:\n{problems}\n\n"
        "Your previous answer was:\n"
        f"{text}\n\n"
        "Reply with corrected JSON only. No explanation, no Markdown fence. "
        "Do not add facts that were not in your previous answer -- correct the "
        "format, not the content."
    )


# -- The subset ------------------------------------------------------------


def _check(value: Any, schema: Mapping[str, Any], *, path: str) -> list[str]:
    """Return every way ``value`` fails ``schema``."""
    where = path or "the answer"
    expected = schema.get("type")
    if expected is not None and not _is_type(value, expected):
        return [f"{where} must be a {expected}"]

    violations: list[str] = []
    if isinstance(value, dict):
        violations += _check_object(value, schema, path=path)
    elif isinstance(value, list):
        violations += _check_array(value, schema, where=where)
    elif isinstance(value, str):
        violations += _check_string(value, schema, where=where)
    elif isinstance(value, (int, float)):
        violations += _check_number(value, schema, where=where)

    allowed = schema.get("enum")
    if allowed is not None and value not in allowed:
        violations.append(f"{where} must be one of {', '.join(str(item) for item in allowed)}")
    return violations


def _check_object(value: dict[str, Any], schema: Mapping[str, Any], *, path: str) -> list[str]:
    """Check required fields, known properties and each property's own schema."""
    violations: list[str] = []
    properties = schema.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}

    for name in schema.get("required", ()):
        if name not in value:
            violations.append(f"{_join(path, name)} is required and was missing")

    if schema.get("additionalProperties") is False:
        for name in value:
            if name not in properties:
                violations.append(f"{_join(path, name)} is not a permitted field")

    for name, child_schema in properties.items():
        if name in value and isinstance(child_schema, Mapping):
            violations += _check(value[name], child_schema, path=_join(path, name))
    return violations


def _check_array(value: list[Any], schema: Mapping[str, Any], *, where: str) -> list[str]:
    """Check length bounds and each element."""
    violations: list[str] = []
    minimum = schema.get("minItems")
    if isinstance(minimum, int) and len(value) < minimum:
        violations.append(f"{where} must have at least {minimum} item(s)")
    maximum = schema.get("maxItems")
    if isinstance(maximum, int) and len(value) > maximum:
        violations.append(f"{where} must have at most {maximum} item(s)")

    items = schema.get("items")
    if isinstance(items, Mapping):
        for index, element in enumerate(value):
            violations += _check(element, items, path=f"{where}[{index}]")
    return violations


def _check_string(value: str, schema: Mapping[str, Any], *, where: str) -> list[str]:
    """Check length bounds."""
    violations: list[str] = []
    minimum = schema.get("minLength")
    if isinstance(minimum, int) and len(value) < minimum:
        violations.append(
            f"{where} must not be empty"
            if minimum == 1
            else f"{where} must be at least {minimum} characters"
        )
    maximum = schema.get("maxLength")
    if isinstance(maximum, int) and len(value) > maximum:
        violations.append(f"{where} must be at most {maximum} characters")
    return violations


def _check_number(value: float, schema: Mapping[str, Any], *, where: str) -> list[str]:
    """Check numeric bounds. This is where a confidence outside zero to one is caught."""
    violations: list[str] = []
    minimum = schema.get("minimum")
    if isinstance(minimum, (int, float)) and value < minimum:
        violations.append(f"{where} must be at least {minimum}")
    maximum = schema.get("maximum")
    if isinstance(maximum, (int, float)) and value > maximum:
        violations.append(f"{where} must be at most {maximum}")
    return violations


def _is_type(value: Any, expected: str) -> bool:
    """Whether a parsed JSON value has a schema type.

    ``bool`` is excluded from the numeric types: Python says ``True`` is an
    ``int``, JSON does not, and a confidence of ``true`` should fail rather than
    become one.
    """
    permitted = _TYPES.get(expected)
    if permitted is None:
        return True
    if expected in {"number", "integer"} and isinstance(value, bool):
        return False
    return isinstance(value, permitted)


def _join(path: str, name: str) -> str:
    """Join a path for a violation message."""
    return f"{path}.{name}" if path else name


__all__ = [
    "MAX_VIOLATIONS",
    "SUPPORTED_KEYWORDS",
    "JsonSchema",
    "ValidationOutcome",
    "build_repair_prompt",
    "parse",
    "require_supported",
    "validate",
]
