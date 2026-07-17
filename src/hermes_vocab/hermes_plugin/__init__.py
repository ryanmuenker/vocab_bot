from __future__ import annotations

from pathlib import Path

from hermes_vocab.capture import CaptureService
from hermes_vocab.config import Settings
from hermes_vocab.database import Database
from hermes_vocab.review import ReviewService

from . import schemas
from .definition import DefinitionProvider
from .gateway import VocabularyGatewayRouter
from .hooks import VocabularyHook
from .tools import ToolHandlers


def register(ctx) -> None:
    settings = Settings.from_environment()
    database = Database(settings.database_path)
    database.initialize()
    capture_service = CaptureService(database)
    review_service = ReviewService(database, settings.timezone)
    handlers = ToolHandlers(capture_service, review_service)
    hook = VocabularyHook(capture_service, review_service)
    ctx.register_auxiliary_task(
        key="vocabulary_definition",
        display_name="Vocabulary definition",
        description="Generate structured senses for an unseen vocabulary entry",
        defaults={"provider": "auto", "timeout": 60},
    )

    if settings.telegram_chat_id is not None:
        async def call_definition_model(**kwargs) -> str:
            from agent.auxiliary_client import (
                async_call_llm,
                extract_content_or_reasoning,
            )

            response = await async_call_llm(**kwargs)
            return extract_content_or_reasoning(response) or ""

        provider = DefinitionProvider(call_definition_model)
        router = VocabularyGatewayRouter(
            capture_service,
            review_service,
            provider,
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
    )
    ctx.register_hook("pre_llm_call", hook.pre_llm_call)
    skill = Path(__file__).parent / "skills" / "vocabulary" / "SKILL.md"
    ctx.register_skill("vocabulary", skill)
