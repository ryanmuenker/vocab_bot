from __future__ import annotations

from pathlib import Path

from hermes_vocab.capture import CaptureService
from hermes_vocab.config import Settings
from hermes_vocab.database import Database
from hermes_vocab.review import ReviewService

from . import schemas
from .hooks import VocabularyHook
from .tools import ToolHandlers


def register(ctx) -> None:
    settings = Settings.from_environment()
    database = Database(settings.database_path)
    database.initialize()
    capture_service = CaptureService(database)
    review_service = ReviewService(database, settings.timezone)
    handlers = ToolHandlers(capture_service, review_service)
    hook = VocabularyHook(review_service)

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
