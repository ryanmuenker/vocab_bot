from __future__ import annotations


SAVE_CARD = {
    "name": "vocabulary_save_card",
    "description": (
        "Persist a new vocabulary entry or distinct sense, or identify an existing "
        "sense. Copy source context verbatim when supplied. For an existing sense, "
        "use the supplied matching sense ID."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "display_text": {"type": "string"},
            "operation": {
                "type": "string",
                "enum": ["new_entry", "new_sense", "existing_sense"],
            },
            "source_context": {"type": "string"},
            "matching_sense_id": {"type": "integer"},
            "part_of_speech": {"type": "string"},
            "definition": {"type": "string"},
            "example_sentence": {"type": "string"},
        },
        "required": ["display_text", "operation"],
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
