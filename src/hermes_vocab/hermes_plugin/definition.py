from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from hermes_vocab.capture import (
    MAX_PART_OF_SPEECH_LENGTH,
    MAX_SENSE_TEXT_LENGTH,
    normalize_sense_identity,
)
from hermes_vocab.models import SenseCard

MAX_SENSES = 20


class DefinitionStatus(StrEnum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    INVALID_RESPONSE = "invalid_response"
    PROVIDER_ERROR = "provider_error"


@dataclass(frozen=True, slots=True)
class DefinitionResult:
    status: DefinitionStatus
    cards: tuple[SenseCard, ...] = ()


def parse_definition_response(text: str) -> DefinitionResult:
    if not isinstance(text, str):
        return DefinitionResult(DefinitionStatus.INVALID_RESPONSE)
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return DefinitionResult(DefinitionStatus.INVALID_RESPONSE)
    if not isinstance(payload, dict):
        return DefinitionResult(DefinitionStatus.INVALID_RESPONSE)
    if payload == {"status": "not_found"}:
        return DefinitionResult(DefinitionStatus.NOT_FOUND)
    if set(payload) != {"senses"}:
        return DefinitionResult(DefinitionStatus.INVALID_RESPONSE)

    raw_senses = payload["senses"]
    if not isinstance(raw_senses, list) or not 1 <= len(raw_senses) <= MAX_SENSES:
        return DefinitionResult(DefinitionStatus.INVALID_RESPONSE)

    validated: list[SenseCard] = []
    for raw_sense in raw_senses:
        if not isinstance(raw_sense, dict) or set(raw_sense) != {
            "part_of_speech",
            "definition",
            "example_sentence",
        }:
            return DefinitionResult(DefinitionStatus.INVALID_RESPONSE)
        fields = (
            raw_sense["part_of_speech"],
            raw_sense["definition"],
            raw_sense["example_sentence"],
        )
        if not all(isinstance(field, str) for field in fields):
            return DefinitionResult(DefinitionStatus.INVALID_RESPONSE)
        part_of_speech, definition, example_sentence = (
            field.strip() for field in fields
        )
        if not (
            0 < len(part_of_speech) <= MAX_PART_OF_SPEECH_LENGTH
            and 0 < len(definition) <= MAX_SENSE_TEXT_LENGTH
            and 0 < len(example_sentence) <= MAX_SENSE_TEXT_LENGTH
        ):
            return DefinitionResult(DefinitionStatus.INVALID_RESPONSE)
        validated.append(SenseCard(part_of_speech, definition, example_sentence))

    cards: list[SenseCard] = []
    seen: set[tuple[str, str]] = set()
    for card in validated:
        identity = normalize_sense_identity(card.part_of_speech, card.definition)
        if identity in seen:
            continue
        seen.add(identity)
        cards.append(card)
    return DefinitionResult(DefinitionStatus.FOUND, tuple(cards))


_SYSTEM_PROMPT = (
    "You are a focused English dictionary enrichment service. "
    "Return JSON only. For a defined entry, return exactly one "
    "top-level key, senses, containing 1 to 20 senses. "
    "List every credible distinct English sense for the supplied "
    "entry, including common, literary, archaic, regional, and "
    "major technical senses. Exclude hyper-specialized jargon and "
    "do not split mere wording variants into separate senses. "
    "Each sense must contain exactly part_of_speech, definition, "
    "and example_sentence. Definitions must be concise and examples "
    "must demonstrate that sense. If the entry is not an English "
    "term or expression, return exactly {\"status\":\"not_found\"}."
)


class DefinitionProvider:
    def __init__(self, call_llm: Callable[..., Awaitable[str]]) -> None:
        self._call_llm = call_llm

    async def define(self, display_text: str) -> DefinitionResult:
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {"display_text": display_text},
                    ensure_ascii=False,
                ),
            },
        ]
        try:
            response = await self._call_llm(
                task="vocabulary_definition",
                messages=messages,
                max_tokens=4000,
                temperature=0,
                tools=[],
            )
        except Exception:
            return DefinitionResult(DefinitionStatus.PROVIDER_ERROR)
        return parse_definition_response(response)
