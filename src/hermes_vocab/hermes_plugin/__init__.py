from __future__ import annotations

from pathlib import Path

from hermes_vocab.capture import CaptureService
from hermes_vocab.config import Settings
from hermes_vocab.database import Database
from hermes_vocab.formatting import format_test_start
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
    database = Database(settings.database_path)
    database.initialize()
    capture_service = CaptureService(database)
    review_service = ReviewService(database, settings.timezone)
    test_service = TestSessionService(database)

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
    )
    hook = VocabularyHook(capture_service, review_service)
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
        def test_command(args: str) -> str:
            if not isinstance(args, str) or args.strip():
                return "Usage: /test"
            return format_test_start(test_service.start())

        ctx.register_command(
            "test",
            test_command,
            description="Start or resume a five-word vocabulary test",
            args_hint="",
        )

        router = VocabularyGatewayRouter(
            capture_service,
            review_service,
            test_service,
            definition_provider,
            evaluation_provider,
            settings.telegram_chat_id,
        )

        async def intercept(**kwargs):
            text = await router.route(**kwargs)
            if text is None:
                return None
            from hermes_cli.plugins import GatewayInterceptResponse

            return GatewayInterceptResponse(text)

        ctx.register_hook("gateway_inbound_intercept", intercept)

    ctx.register_tool(
        name="vocabulary_save_card",
        toolset="vocabulary",
        schema=schemas.SAVE_CARD,
        handler=handlers.save_card,
    )
    ctx.register_tool(
        name="vocabulary_complete_review",
        toolset="vocabulary",
        schema=schemas.COMPLETE_REVIEW,
        handler=handlers.complete_review,
        is_async=True,
    )
    ctx.register_hook("pre_llm_call", hook.pre_llm_call)
    skill = Path(__file__).parent / "skills" / "vocabulary" / "SKILL.md"
    ctx.register_skill("vocabulary", skill)
