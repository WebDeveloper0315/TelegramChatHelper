"""Running one AI task, and recording that it ran.

This is the only place a model is invoked. Everything after this slice -- memory
extraction, summarisation, planning, reply generation -- calls *this*, and gets
the privacy gate, the timeout, the cost accounting and the audit record without
having to remember any of them.

What it does, in order
----------------------

1. Resolve the account, and the chat if one is named.
2. **Ask permission.** A chat's ``ai_processing_mode`` decides whether this
   model may see its content (ADR-024). A refusal is recorded, not merely
   raised: "why did nothing happen" deserves an answer.
3. Invoke the provider, with a timeout the provider does not own.
4. Record the call -- **every outcome, including the failures**. Success-only
   instrumentation hides exactly the expensive cases.

What it does not do
-------------------

**No parsing.** The response is text. What that text is supposed to be belongs
to the task that asked for it, and slice 9b is the first task.

**No retries.** A retry is a policy about failures, and there is no policy yet.
Recording ``rate_limited`` distinctly from ``provider_error`` is this slice's
contribution to whatever policy arrives: it tells them apart.

**No events.** ``DOMAIN_MODEL.md`` section 7 lists ``ProviderUnavailable``, and
nothing subscribes to it. An event shaped before its first consumer is a guess
(the rule slices 5 to 8 followed), and the ``AiCall`` row is the durable record
that matters -- an event is neither durable nor transactional.

The permission rule
-------------------

+-------------------------+---------------------+-------------------------+
| ``ai_processing_mode``  | Local model         | External model          |
+=========================+=====================+=========================+
| ``disabled``            | refused             | refused                 |
+-------------------------+---------------------+-------------------------+
| ``local_only``          | allowed             | refused                 |
+-------------------------+---------------------+-------------------------+
| ``cloud_allowed``       | allowed             | allowed                 |
+-------------------------+---------------------+-------------------------+
| *no chat named*         | allowed             | **refused**             |
+-------------------------+---------------------+-------------------------+

That last row is the one worth arguing about. Content with no chat has no
permission attached to it, and in a local-first application the absence of a
permission is not a permission. A task that genuinely needs an external model
for content that is not a conversation will have to say which chat it belongs
to, or arrive with an ADR explaining why it belongs to none.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tgassist.application.use_cases.account_scope import resolve_account
from tgassist.domain.errors import (
    AiForbiddenError,
    AiProviderError,
    AiRateLimitedError,
    AiResponseError,
    AiTimeoutError,
    RecordNotFoundError,
    SchemaViolationError,
)
from tgassist.domain.model.ai import (
    AiCall,
    AiOutcome,
    PromptVersion,
    TokenUsage,
)
from tgassist.domain.model.chat import AiProcessingMode, Chat
from tgassist.domain.model.identifiers import AccountId, AiCallId, ChatId
from tgassist.domain.model.page import Page
from tgassist.domain.model.query import PageRequest
from tgassist.domain.ports.account_repository import AccountRepository
from tgassist.domain.ports.ai_call_repository import AiCallRepository
from tgassist.domain.ports.ai_provider import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT_SECONDS,
    AiProvider,
    AiRequest,
)
from tgassist.domain.ports.chat_repository import ChatRepository
from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.id_generator import IdGenerator
from tgassist.domain.ports.repository import RepositoryFactory, ScopedRepositoryFactory
from tgassist.domain.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from tgassist.domain.services.structured_output import (
    JsonSchema,
    build_repair_prompt,
    validate,
)

#: Which failures map to which recorded outcome. A table rather than a chain of
#: ``except`` clauses, so that adding an error kind is one line and the mapping
#: can be read at a glance.
_OUTCOMES: dict[type[Exception], AiOutcome] = {
    AiTimeoutError: AiOutcome.TIMEOUT,
    AiRateLimitedError: AiOutcome.RATE_LIMITED,
    AiResponseError: AiOutcome.MALFORMED,
    AiProviderError: AiOutcome.PROVIDER_ERROR,
}


@dataclass(frozen=True, slots=True)
class AiTaskResult:
    """What one task produced.

    Attributes:
        call: The record that was written. Present whatever happened, because a
            record is written whatever happens.
        text: The model's answer, or ``None`` when there was not one.
    """

    call: AiCall
    text: str | None

    @property
    def succeeded(self) -> bool:
        """Whether the model answered."""
        return self.call.succeeded


class ExecuteAiTask:
    """Runs one model invocation and records it.

    The provider is a **constructor** dependency, unlike the Telegram gateway,
    which is passed per call. A gateway holds a live connection with a lifetime;
    a provider is a stateless client over HTTP, so there is nothing to open and
    nothing to close, and injecting it once is what lets every later use case
    take this object rather than a provider plus a set of rules.
    """

    __slots__ = (
        "_accounts",
        "_calls",
        "_chats",
        "_clock",
        "_ids",
        "_keep_responses",
        "_provider",
        "_unit_of_work",
    )

    def __init__(  # noqa: PLR0913, PLR0917 - one collaborator per dependency
        self,
        unit_of_work: UnitOfWorkFactory,
        calls: ScopedRepositoryFactory[AiCallRepository],
        chats: ScopedRepositoryFactory[ChatRepository],
        accounts: RepositoryFactory[AccountRepository],
        provider: AiProvider,
        clock: Clock,
        ids: IdGenerator,
        keep_responses: bool = False,
    ) -> None:
        """Take the collaborators this use case actually needs.

        Args:
            unit_of_work: Transaction factory. One transaction per recorded call.
            calls: AI call repository factory, scoped per account.
            chats: Chat repository factory, scoped per account.
            accounts: Account repository factory.
            provider: The model to invoke. One port, one implementation at a
                time (ADR-057).
            clock: Time source, for the record's timestamp.
            ids: Local identifier generator.
            keep_responses: Whether to store the response text beside its
                digest. Off unless ``ai.store_responses`` is on, which the
                production profile refuses.
        """
        self._unit_of_work = unit_of_work
        self._calls = calls
        self._chats = chats
        self._accounts = accounts
        self._provider = provider
        self._clock = clock
        self._ids = ids
        self._keep_responses = keep_responses

    async def execute(  # noqa: PLR0913 - one argument per thing a request needs
        self,
        *,
        content: str,
        prompt: PromptVersion,
        task_kind: str,
        instructions: str | None = None,
        chat_id: int | None = None,
        account_id: AccountId | None = None,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> AiTaskResult:
        """Run one task and record it.

        Args:
            content: What to act on. Untrusted text belongs here, never in
                ``instructions`` (``SECURITY.md`` section 12).
            prompt: Which prompt at which revision produced this.
            task_kind: What the call is for.
            instructions: The system prompt, if the task has one.
            chat_id: The chat whose content this is about. Naming it is what
                grants permission for an external model.
            account_id: Account to operate on. ``None`` selects the active one.
            max_output_tokens: A ceiling on the answer.
            temperature: Sampling temperature. Zero by default, so a task is
                reproducible unless it asks not to be.
            timeout_seconds: How long to wait.

        Returns:
            What the task produced, and the record that was written.

        Raises:
            RecordNotFoundError: If no account matches, none is active, or this
                account has no such chat.
            AiForbiddenError: If the chat's processing mode does not permit this
                model. A record is written first, so the refusal is auditable.
            DomainValidationError: If the request could not produce an answer.
            AiError: If the provider failed. A record is written first, so the
                failure is measurable.
        """
        request = AiRequest(
            instructions=instructions,
            content=content,
            prompt=prompt,
            task_kind=task_kind,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
        )

        resolved, chat = await self._subject(chat_id, account_id)
        refusal = _refusal(chat, self._provider)
        if refusal is not None:
            call = await self._record(
                resolved, chat, request, outcome=AiOutcome.REFUSED, latency_ms=0
            )
            raise AiForbiddenError(
                refusal,
                user_message=_refusal_message(chat),
                context={
                    "chat_id": int(chat.id) if chat is not None else None,
                    "model": str(self._provider.model),
                    "ai_call_id": int(call.id),
                },
            )

        return await self._invoke(resolved, chat, request)

    # -- Resolution --------------------------------------------------------

    async def _subject(
        self, chat_id: int | None, account_id: AccountId | None
    ) -> tuple[AccountId, Chat | None]:
        """Resolve the account, and the chat this task is about."""
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            if chat_id is None:
                return resolved, None
            return resolved, await self._require_chat(uow, resolved, chat_id)

    async def _require_chat(self, uow: UnitOfWork, account_id: AccountId, chat_id: int) -> Chat:
        """Return the chat this task is about, raising if this account has none."""
        chat = await self._chats(uow, account_id).get(ChatId(chat_id))
        if chat is None:
            msg = f"No chat {chat_id} in account {int(account_id)}"
            raise RecordNotFoundError(
                msg,
                user_message="That chat was not found.",
                context={"chat_id": chat_id, "account_id": int(account_id)},
            )
        return chat

    # -- Invocation --------------------------------------------------------

    async def _invoke(
        self, account_id: AccountId, chat: Chat | None, request: AiRequest
    ) -> AiTaskResult:
        """Call the provider, and record whatever happened.

        The timeout is applied *here* rather than left to the provider, so that
        every implementation is bounded by the same rule and one that ignored
        its own timeout could not hang the application.

        Latency is measured around the whole attempt, including a timeout's full
        wait: a call that was abandoned after sixty seconds took sixty seconds,
        and recording it as instant would hide the slowest thing in the system.
        """
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(
                self._provider.generate(request), timeout=request.timeout_seconds
            )
        except TimeoutError as exc:
            # asyncio.wait_for raises the builtin. Normalised here so the
            # recorded outcome and the raised error agree, and so a caller sees
            # the AI taxonomy rather than a bare TimeoutError.
            call = await self._record(
                account_id,
                chat,
                request,
                outcome=AiOutcome.TIMEOUT,
                latency_ms=_elapsed_ms(started),
            )
            msg = f"The model did not answer within {request.timeout_seconds}s"
            raise AiTimeoutError(
                msg,
                user_message="The model took too long to answer.",
                context={"ai_call_id": int(call.id), "task_kind": request.task_kind},
            ) from exc
        except asyncio.CancelledError:
            # The caller went away. Recorded because the tokens were still
            # spent, and re-raised untouched so shutdown is not delayed.
            await self._record(
                account_id,
                chat,
                request,
                outcome=AiOutcome.CANCELLED,
                latency_ms=_elapsed_ms(started),
            )
            raise
        except tuple(_OUTCOMES) as exc:
            call = await self._record(
                account_id,
                chat,
                request,
                outcome=_OUTCOMES[type(exc)],
                latency_ms=_elapsed_ms(started),
            )
            raise _with_call(exc, call.id) from exc

        call = await self._record(
            account_id,
            chat,
            request,
            outcome=AiOutcome.SUCCESS,
            latency_ms=_elapsed_ms(started),
            usage=response.usage,
            finish_reason=response.finish_reason,
            response=response.text,
        )
        return AiTaskResult(call=call, text=response.text)

    async def _record(  # noqa: PLR0913 - one argument per recorded field
        self,
        account_id: AccountId,
        chat: Chat | None,
        request: AiRequest,
        *,
        outcome: AiOutcome,
        latency_ms: int,
        usage: TokenUsage | None = None,
        finish_reason: object = None,
        response: str | None = None,
    ) -> AiCall:
        """Write the record of one call, in a transaction of its own.

        Its own transaction, and not the caller's: the record must survive
        whatever the caller does next. A failure that rolled back its own
        instrumentation would make the expensive calls precisely the ones with
        no evidence.
        """
        call = AiCall.record(
            call_id=AiCallId(self._ids.new_id()),
            account_id=account_id,
            chat_id=chat.id if chat is not None else None,
            model=self._provider.model,
            prompt=request.prompt,
            task_kind=request.task_kind,
            outcome=outcome,
            latency_ms=latency_ms,
            now=self._clock.now(),
            usage=usage,
            finish_reason=finish_reason,  # type: ignore[arg-type]
            response=response,
            keep_response=self._keep_responses,
        )
        async with self._unit_of_work() as uow:
            await self._calls(uow, account_id).add(call)
            await uow.commit()
        return call


@dataclass(frozen=True, slots=True)
class StructuredAnswer:
    """A validated answer, and what it took to get one.

    Attributes:
        payload: What the model returned, having satisfied its schema.
        call_id: The call whose answer was used -- the *second* one when a
            repair was needed, because that is the answer that was accepted.
        repaired: Whether the first attempt failed validation. Worth surfacing:
            a prompt that needs repairing often is a prompt that needs
            rewriting.
    """

    payload: Mapping[str, Any]
    call_id: AiCallId
    repaired: bool


class StructuredAiTask:
    """Runs one AI task that must return a particular shape, repairing once.

    Wraps :class:`ExecuteAiTask` rather than replacing it: the gate, the
    timeout, the accounting and the audit record all still belong there. What
    this adds is the one rule every structured task shares -- validate, and on
    failure hand the model its own answer back exactly once (ADR-020 section 4).

    It exists because there are now two such tasks, and "exactly one repair" is
    a rule that must not be able to become "exactly one repair here and two
    there" (ADR-061). Everything about *what* is asked stays with the caller;
    only the loop is here.
    """

    __slots__ = ("_task",)

    def __init__(self, task: ExecuteAiTask) -> None:
        """Take the execution boundary this runs through."""
        self._task = task

    async def execute(  # noqa: PLR0913 - one argument per thing a call needs
        self,
        *,
        content: str,
        instructions: str | None,
        prompt: PromptVersion,
        task_kind: str,
        schema: JsonSchema,
        chat_id: int | None = None,
        account_id: AccountId | None = None,
    ) -> StructuredAnswer:
        """Ask, validate, and repair at most once.

        The repair is a second call, so it is recorded and costed like any
        other. A second failure is not retried: the model has now been told
        twice, and a third attempt costs money to arrive in the same place.

        Raises:
            SchemaViolationError: If both attempts failed validation. The
                violations travel in the context; the payload does not, because
                model output about a conversation is conversation content
                (``SECURITY.md`` section 9).
            AiError: If the provider failed, unchanged from ``ExecuteAiTask``.
        """
        first = await self._task.execute(
            content=content,
            instructions=instructions,
            prompt=prompt,
            task_kind=task_kind,
            chat_id=chat_id,
            account_id=account_id,
        )
        outcome = validate(first.text or "", schema)
        if outcome.payload is not None:
            return StructuredAnswer(outcome.payload, first.call.id, repaired=False)

        second = await self._task.execute(
            content=build_repair_prompt(first.text or "", outcome.violations),
            instructions=instructions,
            prompt=prompt,
            task_kind=task_kind,
            chat_id=chat_id,
            account_id=account_id,
        )
        repaired = validate(second.text or "", schema)
        if repaired.payload is not None:
            return StructuredAnswer(repaired.payload, second.call.id, repaired=True)

        msg = f"The model's answer did not satisfy {schema.id} after one repair attempt"
        raise SchemaViolationError(
            msg,
            user_message="The model's answer could not be read, and correcting it did not help.",
            context={
                "schema": schema.id,
                "prompt": str(prompt),
                "violations": list(repaired.violations),
                "ai_call_id": int(second.call.id),
            },
        )


class GetAiCall:
    """Looks one recorded call up."""

    __slots__ = ("_accounts", "_calls", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        calls: ScopedRepositoryFactory[AiCallRepository],
        accounts: RepositoryFactory[AccountRepository],
    ) -> None:
        """Take the collaborators this query actually needs."""
        self._unit_of_work = unit_of_work
        self._calls = calls
        self._accounts = accounts

    async def execute(self, call_id: int, *, account_id: AccountId | None = None) -> AiCall | None:
        """Return one call, or ``None`` if this account has no such call."""
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            return await self._calls(uow, resolved).get(AiCallId(call_id))


class ListAiCalls:
    """Returns a page of this account's recorded calls."""

    __slots__ = ("_accounts", "_calls", "_unit_of_work")

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        calls: ScopedRepositoryFactory[AiCallRepository],
        accounts: RepositoryFactory[AccountRepository],
    ) -> None:
        """Take the collaborators this query actually needs."""
        self._unit_of_work = unit_of_work
        self._calls = calls
        self._accounts = accounts

    async def execute(
        self, request: PageRequest | None = None, *, account_id: AccountId | None = None
    ) -> Page[AiCall]:
        """Return one page of calls, newest first."""
        async with self._unit_of_work() as uow:
            resolved = await resolve_account(self._accounts(uow), account_id)
            return await self._calls(uow, resolved).list_recent(request or PageRequest())


def _refusal(chat: Chat | None, provider: AiProvider) -> str | None:
    """Return why this model may not run on this content, or ``None``.

    The privacy gate (ADR-024), in one function, so that no caller decides for
    itself and no later task can forget to ask.
    """
    external = provider.model.is_external

    if chat is None:
        # Content with no chat has no permission attached, and in a local-first
        # application the absence of a permission is not a permission.
        if external:
            return (
                f"{provider.model} is an external model and this task names no chat, "
                f"so nothing has granted permission to transmit its content"
            )
        return None

    if chat.ai_processing_mode is AiProcessingMode.DISABLED:
        return f"Chat {int(chat.id)} has AI processing switched off"
    if external and not chat.allows_cloud_ai:
        return (
            f"Chat {int(chat.id)} is {chat.ai_processing_mode.value} and "
            f"{provider.model} is an external model"
        )
    return None


def _refusal_message(chat: Chat | None) -> str:
    """Return the sentence a person reads when the gate refuses."""
    if chat is None:
        return (
            "A cloud model cannot be used for content that is not part of a chat. "
            "Name the chat it belongs to, or configure a local model."
        )
    if chat.ai_processing_mode is AiProcessingMode.DISABLED:
        return "That chat has AI processing switched off. Turn it on with `tgassist chat set`."
    return (
        "That chat is set to local-only processing, and the configured model is a "
        "cloud one. Allow cloud processing for the chat, or configure a local model."
    )


def _with_call(error: Exception, call_id: AiCallId) -> Exception:
    """Return the error with the recorded call's identifier attached.

    So that a user reading a failure can find the row that records it, and a
    developer can compare two failures without either of them carrying content.
    """
    context = getattr(error, "context", None)
    if isinstance(context, dict):
        context["ai_call_id"] = int(call_id)
    return error


def _elapsed_ms(started: float) -> int:
    """Return how long has passed, in whole milliseconds."""
    return max(0, int((time.monotonic() - started) * 1000))


__all__ = [
    "AiTaskResult",
    "ExecuteAiTask",
    "GetAiCall",
    "ListAiCalls",
    "StructuredAiTask",
    "StructuredAnswer",
]
