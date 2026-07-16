from hermes_vocab.capture import parse_capture_message
from hermes_vocab.models import CaptureRequest


def test_one_word_produces_context_free_request() -> None:
    assert parse_capture_message("  obdurate  ") == CaptureRequest("obdurate", None)


def test_first_line_word_and_remaining_text_produce_context() -> None:
    assert parse_capture_message(
        "\nbank\nShe sat on the bank.\nThe river was high.\n"
    ) == CaptureRequest(
        "bank",
        "She sat on the bank.\nThe river was high.",
    )


def test_non_lexical_first_line_is_not_capture() -> None:
    assert parse_capture_message("How are you?\nI am reading.") is None


def test_command_is_not_capture() -> None:
    assert parse_capture_message("/help\nword") is None


def test_internal_blank_context_lines_are_preserved() -> None:
    assert parse_capture_message("bank\nfirst\n\nsecond") == CaptureRequest(
        "bank", "first\n\nsecond"
    )
