from __future__ import annotations

import asyncio
import json

import pytest

from hermes_vocab.hermes_plugin.definition import (
    DefinitionProvider,
    DefinitionStatus,
    parse_definition_response,
)


def valid_response() -> str:
    return json.dumps(
        {
            "senses": [
                {
                    "part_of_speech": "adjective",
                    "definition": "Provided as a matter of form.",
                    "example_sentence": "The vote was pro forma.",
                },
                {
                    "part_of_speech": "noun",
                    "definition": "A projected financial statement.",
                    "example_sentence": "She prepared a pro forma.",
                },
            ]
        }
    )


def test_parse_definition_response_returns_ordered_senses() -> None:
    result = parse_definition_response(valid_response())

    assert result.status is DefinitionStatus.FOUND
    assert [card.part_of_speech for card in result.cards] == ["adjective", "noun"]


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        "```json\n" + valid_response() + "\n```",
        "prefix " + valid_response(),
        json.dumps([]),
        json.dumps({}),
        json.dumps({"senses": [], "extra": True}),
        json.dumps({"senses": "not-a-list"}),
        json.dumps({"senses": []}),
        json.dumps({"status": "not_found", "senses": []}),
        json.dumps(
            {
                "senses": [
                    {
                        "part_of_speech": "noun",
                        "definition": "definition",
                    }
                ]
            }
        ),
        json.dumps(
            {
                "senses": [
                    {
                        "part_of_speech": "noun",
                        "definition": "definition",
                        "example_sentence": "example",
                        "extra": True,
                    }
                ]
            }
        ),
        json.dumps(
            {
                "senses": [
                    {
                        "part_of_speech": 1,
                        "definition": "definition",
                        "example_sentence": "example",
                    }
                ]
            }
        ),
        json.dumps(
            {
                "senses": [
                    {
                        "part_of_speech": " ",
                        "definition": "definition",
                        "example_sentence": "example",
                    }
                ]
            }
        ),
        json.dumps(
            {
                "senses": [
                    {
                        "part_of_speech": "x" * 51,
                        "definition": "definition",
                        "example_sentence": "example",
                    }
                ]
            }
        ),
        json.dumps(
            {
                "senses": [
                    {
                        "part_of_speech": "noun",
                        "definition": "x" * 501,
                        "example_sentence": "example",
                    }
                ]
            }
        ),
        json.dumps(
            {
                "senses": [
                    {
                        "part_of_speech": "noun",
                        "definition": "definition",
                        "example_sentence": "x" * 501,
                    }
                ]
            }
        ),
        json.dumps(
            {
                "senses": [
                    {
                        "part_of_speech": "noun",
                        "definition": str(index),
                        "example_sentence": "example",
                    }
                    for index in range(21)
                ]
            }
        ),
    ],
)
def test_parse_definition_response_rejects_non_exact_schemas(response: str) -> None:
    assert parse_definition_response(response).status is DefinitionStatus.INVALID_RESPONSE


def test_parse_definition_response_deduplicates_after_validating_all_cards() -> None:
    response = json.dumps(
        {
            "senses": [
                {
                    "part_of_speech": " Noun ",
                    "definition": " A projected financial statement. ",
                    "example_sentence": "First example.",
                },
                {
                    "part_of_speech": "noun",
                    "definition": "A projected   financial statement.",
                    "example_sentence": "Duplicate example.",
                },
                {
                    "part_of_speech": "adjective",
                    "definition": "Provided as a matter of form.",
                    "example_sentence": "The vote was pro forma.",
                },
            ]
        }
    )

    result = parse_definition_response(response)

    assert result.status is DefinitionStatus.FOUND
    assert [card.example_sentence for card in result.cards] == [
        "First example.",
        "The vote was pro forma.",
    ]


def test_parse_definition_response_accepts_exact_not_found() -> None:
    result = parse_definition_response('{"status":"not_found"}')

    assert result.status is DefinitionStatus.NOT_FOUND
    assert result.cards == ()


def test_definition_provider_makes_one_bounded_tool_free_call() -> None:
    calls: list[dict] = []

    async def call_llm(**kwargs) -> str:
        calls.append(kwargs)
        return valid_response()

    result = asyncio.run(DefinitionProvider(call_llm).define("Pro Forma"))

    assert result.status is DefinitionStatus.FOUND
    assert len(calls) == 1
    assert calls[0]["task"] == "vocabulary_definition"
    assert calls[0]["max_tokens"] == 4000
    assert calls[0]["temperature"] == 0
    assert calls[0]["tools"] == []
    assert json.loads(calls[0]["messages"][1]["content"]) == {
        "display_text": "Pro Forma"
    }
    assert "Pro Forma" not in calls[0]["messages"][0]["content"]


@pytest.mark.parametrize(
    ("response", "expected"),
    [("", DefinitionStatus.INVALID_RESPONSE), ("not json", DefinitionStatus.INVALID_RESPONSE)],
)
def test_definition_provider_returns_invalid_response_without_retry(
    response: str,
    expected: DefinitionStatus,
) -> None:
    calls = 0

    async def call_llm(**kwargs) -> str:
        nonlocal calls
        calls += 1
        return response

    result = asyncio.run(DefinitionProvider(call_llm).define("Pro Forma"))

    assert result.status is expected
    assert calls == 1


def test_definition_provider_translates_provider_exception_without_retry() -> None:
    calls = 0

    async def call_llm(**kwargs) -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("provider unavailable")

    result = asyncio.run(DefinitionProvider(call_llm).define("Pro Forma"))

    assert result.status is DefinitionStatus.PROVIDER_ERROR
    assert calls == 1
