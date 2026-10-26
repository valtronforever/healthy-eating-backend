"""Unit tests for the deterministic parts of the evaluation suite (evals/)."""

import json

import pytest

from evals.proxy import _parse_tool_call, _record_result
from evals.scenarios import confirms_saved, reports_not_saved


@pytest.mark.parametrize(
    ("reply", "saved", "not_saved"),
    [
        ("Lunch: chicken, 520 kcal.\n\nSaved to HealthyEating.", True, False),
        ("Saved to HealthyEating: Salmon, 610 kcal (protein 42 g, fat 28 g).", True, False),
        ("Updated to 3 slices.\nSaved the correction to HealthyEating.", True, False),
        ("Updated in HealthyEating: pizza, 855 kcal.", True, False),
        # A decimal point does not end the sentence.
        ("Saved 177.4 lb to HealthyEating.", True, False),
        ("Imported 4 meals for Oct 1\u20133. They\u2019re in HealthyEating.", True, False),
        ("They're not in HealthyEating yet.", False, False),
        ("Was the drink part of it?\n\nNot saved to HealthyEating yet.", False, True),
        ("I couldn't save this to HealthyEating because of a server error.", False, True),
        ("The meal wasn't saved to HealthyEating: 503 Service Unavailable.", False, True),
        # A save claim without naming HealthyEating does not count (it may be invented).
        ("Logged lunch: tuna salad, 350 kcal.", False, False),
    ],
)
def test_reply_classification(reply: str, saved: bool, not_saved: bool) -> None:
    assert confirms_saved(reply) is saved
    assert reports_not_saved(reply) is not_saved


def test_parse_tool_call_ignores_other_methods() -> None:
    assert _parse_tool_call(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}') is None
    assert _parse_tool_call(b"not json") is None


def test_parse_tool_call() -> None:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "log_meal", "arguments": {"dish_name": "Soup"}},
        }
    ).encode()
    call = _parse_tool_call(body)
    assert call is not None
    assert call.name == "log_meal"
    assert call.arguments == {"dish_name": "Soup"}


def test_record_result_from_event_stream() -> None:
    call = _parse_tool_call(b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"x"}}')
    assert call is not None
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [], "structuredContent": {"status": "logged"}, "isError": False},
    }
    _record_result(
        call, f"event: message\ndata: {json.dumps(message)}\n\n".encode(), "text/event-stream"
    )
    assert call.structured_result == {"status": "logged"}
    assert call.is_error is False


def test_record_result_tool_error() -> None:
    call = _parse_tool_call(b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"x"}}')
    assert call is not None
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": "Not saved"}], "isError": True},
    }
    _record_result(call, json.dumps(message).encode(), "application/json")
    assert call.is_error is True
    assert call.error_text == "Not saved"
