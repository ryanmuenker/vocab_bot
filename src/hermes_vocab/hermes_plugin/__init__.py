from __future__ import annotations

from pathlib import Path

from hermes_vocab.capture import CaptureService
from hermes_vocab.config import ConfigurationError, Settings
from hermes_vocab.database import Database
from hermes_vocab.models import (
    CardDirection,
    StudyMode,
    StudyMutationStatus,
    StudyStartStatus,
)
from hermes_vocab.review import ReviewService
from hermes_vocab.test_session import TestSessionService

from . import schemas
from .definition import DefinitionProvider
from .evaluation import EvaluationProvider
from .gateway import VocabularyGatewayRouter
from .hooks import VocabularyHook
from .tools import ToolHandlers


def register(ctx) -> None:
    settings = Settings.from_environment()
    try:
        from hermes_cli.plugin_contracts import (
            GatewayInterceptResponse,
            OutboundResponse,
            PluginCommandSource,
        )
        from hermes_cli.plugins import VALID_HOOKS
    except (ImportError, AttributeError) as error:
        raise ConfigurationError(
            "Installed Hermes does not expose delivery-safe plugin contracts"
        ) from error
    required_hooks = {"gateway_inbound_intercept", "post_outbound_delivery"}
    if not required_hooks.issubset(VALID_HOOKS):
        raise ConfigurationError(
            "Installed Hermes does not expose the outbound receipt hook"
        )

    database = Database(settings.database_path)
    database.initialize()
    capture_service = CaptureService(database)
    review_service = ReviewService(database, settings.timezone)
    test_service = TestSessionService(database, settings.timezone)

    async def call_auxiliary_model(**kwargs) -> str:
        from agent.auxiliary_client import (
            async_call_llm,
            extract_content_or_reasoning,
        )

        response = await async_call_llm(**kwargs)
        return extract_content_or_reasoning(response) or ""

    definition_provider = DefinitionProvider(call_auxiliary_model)
    evaluation_provider = EvaluationProvider(call_auxiliary_model)
    handlers = ToolHandlers(
        capture_service,
        review_service,
        evaluation_provider,
        test_service,
    )
    hook = VocabularyHook(
        capture_service,
        review_service,
        settings.telegram_chat_id,
    )
    ctx.register_auxiliary_task(
        key="vocabulary_definition",
        display_name="Vocabulary definition",
        description="Generate structured senses for an unseen vocabulary entry",
        defaults={"provider": "auto", "timeout": 60},
    )
    ctx.register_auxiliary_task(
        key="vocabulary_answer_evaluation",
        display_name="Vocabulary answer evaluation",
        description="Evaluate a learner response against stored vocabulary senses",
        defaults={"provider": "auto", "timeout": 60},
    )

    if settings.telegram_chat_id is not None:
        router = VocabularyGatewayRouter(
            capture_service,
            review_service,
            test_service,
            definition_provider,
            evaluation_provider,
            settings.telegram_chat_id,
            hook,
        )

        def is_root_source(source) -> bool:
            return (
                isinstance(source, PluginCommandSource)
                and source.authenticated is True
                and source.platform == "telegram"
                and source.chat_id == str(settings.telegram_chat_id)
                and source.chat_type == "dm"
                and source.thread_id is None
            )

        def source_error() -> str:
            return (
                "Vocabulary study is available only in the configured "
                "Telegram root DM."
            )

        def review_command(args: str, *, source) -> str | object:
            if not is_root_source(source):
                return source_error()
            if not isinstance(args, str) or args.strip():
                return "Usage: /review"
            result = review_service.start()
            if result.status in {
                StudyStartStatus.STARTED,
                StudyStartStatus.RESUMED,
            }:
                prompt = router.prepare_review_prompt()
                if prompt is None:
                    return "I couldn't prepare the review. Please try again."
                correlated = router.correlate(prompt, prompt.prompt_text)
                return OutboundResponse(str(correlated), correlated.correlation_id)
            if result.status is StudyStartStatus.EMPTY:
                return "There are no eligible vocabulary cards to review."
            if result.status is StudyStartStatus.CONFLICT:
                return "Finish or exit your active test first."
            return "I couldn't start the review. Please try again."

        def test_command(args: str, *, source) -> str | object:
            usage = (
                "Usage: /test forward|reverse\n"
                "Forward: recall each saved meaning from its word.\n"
                "Reverse: recall the saved word from one exact definition."
            )
            if not is_root_source(source):
                return source_error()
            if not isinstance(args, str):
                return usage
            try:
                direction = CardDirection(args.strip())
            except ValueError:
                return usage
            result = test_service.start(direction)
            if result.status in {
                StudyStartStatus.STARTED,
                StudyStartStatus.RESUMED,
            }:
                if result.snapshot is None or result.snapshot.current_prompt is None:
                    return "I couldn't prepare the test. Please try again."
                prompt = result.snapshot.current_prompt
                correlated = router.correlate(prompt, prompt.prompt_text)
                return OutboundResponse(str(correlated), correlated.correlation_id)
            if result.status is StudyStartStatus.EMPTY:
                available = result.available_count or 0
                missing = max(5 - available, 0)
                return (
                    f"You have {available} eligible distinct {direction.value} "
                    f"entries. Add or unbury {missing} more to start."
                )
            if result.status is StudyStartStatus.CONFLICT:
                active = review_service.active_mode()
                active_text = (
                    "review"
                    if active is StudyMode.REVIEW
                    else active.value.replace("test_", "") + " test"
                    if active is not None
                    else "study session"
                )
                return f"Finish or exit your active {active_text} first."
            return "I couldn't start the test. Please try again."

        def endstudy_command(args: str, *, source) -> str:
            if not is_root_source(source):
                return source_error()
            if not isinstance(args, str) or args.strip():
                return "Usage: /endstudy"
            if review_service.active_mode() is None:
                return "There is no active vocabulary study session."
            if review_service.exit() is not StudyMutationStatus.COMPLETED:
                return "I couldn't exit that session. Please try again."
            return "Review exited. Unfinished cards are still due."

        ctx.register_command(
            "review",
            review_command,
            description="Start or resume delivery-safe vocabulary review",
            source_aware=True,
        )
        ctx.register_command(
            "test",
            test_command,
            description="Start or resume a five-card directional vocabulary test",
            args_hint="forward|reverse",
            source_aware=True,
        )
        ctx.register_command(
            "endstudy",
            endstudy_command,
            description="Exit the active vocabulary review or test",
            source_aware=True,
        )

        async def intercept(**kwargs):
            text = await router.route(**kwargs)
            if text is None:
                return None
            return GatewayInterceptResponse(
                str(text),
                correlation_id=getattr(text, "correlation_id", None),
            )

        ctx.register_hook("gateway_inbound_intercept", intercept)
        ctx.register_hook("post_outbound_delivery", hook.post_outbound_delivery)

    ctx.register_tool(
        name="vocabulary_save_card",
        toolset="vocabulary",
        schema=schemas.SAVE_CARD,
        handler=handlers.save_card,
    )
    ctx.register_tool(
        name="vocabulary_continue_study",
        toolset="vocabulary",
        schema=schemas.CONTINUE_STUDY,
        handler=handlers.continue_study,
        is_async=True,
    )
    ctx.register_hook("pre_llm_call", hook.pre_llm_call)
    skill = Path(__file__).parent / "skills" / "vocabulary" / "SKILL.md"
    ctx.register_skill("vocabulary", skill)
