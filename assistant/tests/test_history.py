import json

import pytest
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from assistant.views import HISTORY_LIMIT

from .conftest import reply

pytestmark = pytest.mark.django_db

ASK, HISTORY, CLEAR = (
    reverse("assistant:ask"),
    reverse("assistant:history"),
    reverse("assistant:clear_history"),
)


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()


def ask(client, question="What is the due date?", **extra):
    return client.post(
        ASK, json.dumps({"question": question, **extra}), content_type="application/json"
    )


def history(client):
    return client.get(HISTORY).json()["messages"]


def test_questions_and_answers_are_kept_across_pages(librarian_client, openrouter):
    openrouter.responses = [reply('{"answer": "When the book must be back.", "sources": []}')]
    ask(librarian_client, "What is the due date?", client_id="abc-123")
    messages = history(librarian_client)
    assert [m["role"] for m in messages] == ["user", "bot"]
    assert messages[0]["text"] == "What is the due date?"
    assert messages[1]["answer"] == "When the book must be back."
    assert messages[1]["client_id"] == "abc-123"  # lets a new page find a pending answer


def test_answer_is_saved_even_if_nobody_reads_the_response(librarian, openrouter):
    """The user navigates away mid-question: the request still completes on the
    server and the answer lands in the session for the next page."""
    openrouter.responses = [reply('{"answer": "Saved for later.", "sources": []}')]
    tab = Client()
    tab.force_login(librarian)
    ask(tab, client_id="left-page")  # response ignored, as if the page was closed
    next_page = history(tab)
    assert next_page[-1]["client_id"] == "left-page"
    assert next_page[-1]["answer"] == "Saved for later."


def test_history_is_capped(librarian_client, openrouter, settings):
    settings.ASSISTANT_RATE_PER_MINUTE = 100  # not what this test is about
    openrouter.responses = [reply('{"answer": "ok", "sources": []}')] * (HISTORY_LIMIT // 2 + 3)
    for i in range(HISTORY_LIMIT // 2 + 3):
        ask(librarian_client, f"question number {i}")
    messages = history(librarian_client)
    assert len(messages) == HISTORY_LIMIT
    assert messages[-2]["text"] == f"question number {HISTORY_LIMIT // 2 + 2}"


def test_errors_are_not_stored(librarian_client):
    ask(librarian_client, "x")  # too short: 400
    assert history(librarian_client) == []


def test_clear(librarian_client, openrouter):
    openrouter.responses = [reply('{"answer": "ok", "sources": []}')]
    ask(librarian_client)
    assert librarian_client.post(CLEAR).status_code == 200
    assert history(librarian_client) == []


def test_history_is_private_to_the_login_session(librarian, admin_user, openrouter):
    openrouter.responses = [reply('{"answer": "secret", "sources": []}')]
    mine, other_user, my_other_browser = Client(), Client(), Client()
    mine.force_login(librarian)
    other_user.force_login(admin_user)
    my_other_browser.force_login(librarian)  # a separate session for the same user
    ask(mine)
    assert history(other_user) == []
    assert history(my_other_browser) == []


def test_logout_deletes_the_history(librarian_client, openrouter):
    openrouter.responses = [reply('{"answer": "ok", "sources": []}')]
    ask(librarian_client)
    key = librarian_client.session.session_key
    librarian_client.post(reverse("admin:logout"))
    assert not Session.objects.filter(session_key=key).exists()


def test_history_endpoints_need_staff(client):
    assert client.get(HISTORY).status_code == 403
    assert client.post(CLEAR).status_code == 403


def test_clear_needs_csrf(librarian):
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(librarian)
    assert strict.post(CLEAR).status_code == 403


def test_bad_client_id_is_dropped(librarian_client, openrouter):
    openrouter.responses = [reply('{"answer": "ok", "sources": []}')]
    ask(librarian_client, client_id="<script>")
    assert history(librarian_client)[-1]["client_id"] is None
