from __future__ import annotations

from hermes_vocab.capture import is_lexical_word
from hermes_vocab.review import ReviewService


class VocabularyHook:
    def __init__(self, review_service: ReviewService) -> None:
        self.review_service = review_service

    def pre_llm_call(
        self,
        session_id: str,
        user_message: str,
        conversation_history: list,
        is_first_turn: bool,
        model: str,
        platform: str,
        **kwargs,
    ):
        if platform != "telegram" or user_message.lstrip().startswith("/"):
            return None
        if self.review_service.has_pending_review():
            return (
                "A vocabulary review is pending in SQLite. Load the "
                "vocabulary:vocabulary plugin skill, treat the user's original "
                "message as the raw review response, call "
                "vocabulary_complete_review, and relay the returned text "
                "verbatim. Do not grade the answer."
            )
        if is_lexical_word(user_message):
            return (
                "This Telegram message is a vocabulary capture. Load the "
                "vocabulary:vocabulary plugin skill, generate one concise card, "
                "call vocabulary_save_card, and relay the returned text verbatim."
            )
        return None
