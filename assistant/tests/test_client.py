import httpx
import pytest
from django.test import override_settings

from assistant import client

from .conftest import reply

KNOWN = {"loan.due_date", "book.isbn"}
MESSAGES = [{"role": "user", "content": "hi"}]


def test_parse_valid_json_keeps_only_real_sources():
    answer, sources = client.parse_reply(
        '{"answer": "Return date.", "sources": ["loan.due_date", "loan.made_up", 7]}', KNOWN
    )
    assert answer == "Return date."
    assert sources == ["loan.due_date"]


def test_parse_json_inside_code_fence():
    answer, sources = client.parse_reply(
        '```json\n{"answer": "Thirteen digits.", "sources": ["book.isbn"]}\n```', KNOWN
    )
    assert (answer, sources) == ("Thirteen digits.", ["book.isbn"])


def test_parse_salvages_cut_off_json():
    raw = '{"answer": "You can extend it by up to 7 days, see loan.due_date and \\"extended\\'
    answer, sources = client.parse_reply(raw, KNOWN)
    assert answer == 'You can extend it by up to 7 days, see loan.due_date and "extended'
    assert sources == ["loan.due_date"]


def test_parse_salvages_json_with_trailing_garbage():
    raw = '{"answer": "Thirteen digits.", "sources": ["book.isbn"],}'
    assert client.parse_reply(raw, KNOWN) == ("Thirteen digits.", ["book.isbn"])


@pytest.mark.parametrize("raw", ["Just prose, no JSON.", '{"answer": 5}', "{broken json"])
def test_parse_non_json_falls_back_to_raw_text(raw):
    answer, sources = client.parse_reply(raw, KNOWN)
    assert answer == raw.strip()
    assert sources == []


def test_parse_truncates_long_answers():
    answer, _ = client.parse_reply("x" * 5000, KNOWN)
    assert len(answer) == client.MAX_ANSWER_CHARS


def test_first_model_success(openrouter):
    openrouter.responses = [reply("hello")]
    result = client.complete(MESSAGES)
    assert (result.content, result.model) == ("hello", "test/model-a:free")


def test_429_retries_once_then_falls_back_to_next_model(openrouter):
    openrouter.responses = [httpx.Response(429), httpx.Response(429), reply("from b")]
    result = client.complete(MESSAGES)
    assert result.model == "test/model-b:free"
    assert openrouter.models_called == ["test/model-a:free"] * 2 + ["test/model-b:free"]


def test_timeout_moves_straight_to_next_model(openrouter):
    openrouter.responses = [httpx.ReadTimeout("slow"), reply("from b")]
    assert client.complete(MESSAGES).model == "test/model-b:free"


@pytest.mark.parametrize(
    "bad",
    [
        reply("   "),
        httpx.Response(200, json={"unexpected": True}),
        httpx.Response(200, text="<html>"),
        httpx.Response(401),
    ],
)
def test_unusable_reply_moves_to_next_model(openrouter, bad):
    openrouter.responses = [bad, reply("from b")]
    assert client.complete(MESSAGES).model == "test/model-b:free"


def test_all_models_failing_raises(openrouter):
    openrouter.responses = [httpx.ConnectError("down"), httpx.ReadTimeout("slow")]
    with pytest.raises(client.AssistantUnavailable):
        client.complete(MESSAGES)


def test_time_budget_stops_the_chain(openrouter, monkeypatch):
    monkeypatch.setattr(client, "TOTAL_BUDGET", 0)
    with pytest.raises(client.AssistantUnavailable, match="budget"):
        client.complete(MESSAGES)
    assert openrouter.requests == []


@override_settings(OPENROUTER_API_KEY="")
def test_missing_key_is_not_configured(openrouter):
    with pytest.raises(client.AssistantUnavailable, match="not configured"):
        client.complete(MESSAGES)
    assert openrouter.requests == []


def test_request_shape(openrouter):
    openrouter.responses = [reply("ok")]
    client.complete(MESSAGES)
    sent = openrouter.requests[0]
    assert sent["temperature"] == 0.2
    assert sent["max_tokens"] == 300
    assert sent["messages"] == MESSAGES
