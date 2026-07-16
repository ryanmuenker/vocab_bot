from __future__ import annotations


SAVE_CARD = {
    "name": "vocabulary_save_card",
    "description": (
        "Persist one complete vocabulary card. Use only after generating one concise "
        "definition, part of speech, and example for a single word."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "word": {"type": "string"},
            "part_of_speech": {"type": "string"},
            "definition": {"type": "string"},
            "example_sentence": {"type": "string"},
        },
        "required": [
            "word",
            "part_of_speech",
            "definition",
            "example_sentence",
        ],
    },
}

COMPLETE_REVIEW = {
    "name": "vocabulary_complete_review",
    "description": (
        "Record the user's raw response to the currently pending vocabulary review "
        "and return the stored definition and example. Do not grade the response."
    ),
    "parameters": {
        "type": "object",
        "properties": {"answer_text": {"type": "string"}},
        "required": ["answer_text"],
    },
}
