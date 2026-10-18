import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from assistant import lookups
from library.models import Member
from library.tests.factories import BookFactory, LoanFactory, MemberFactory

from .conftest import reply

pytestmark = pytest.mark.django_db
URL = reverse("assistant:ask")


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()


def late_loan(member, days_late, **kwargs):
    today = timezone.localdate()
    return LoanFactory(
        member=member,
        loaned_on=today - timedelta(days=days_late + 14),
        due_date=today - timedelta(days=days_late),
        **kwargs,
    )


def ask(client, question):
    return client.post(URL, json.dumps({"question": question}), content_type="application/json")


# --- fees due ----------------------------------------------------------------


def test_fees_due_sums_open_overdue_loans_only():
    member = MemberFactory()
    late_loan(member, 4)
    late_loan(member, 2)
    LoanFactory(member=member)  # not overdue
    late_loan(member, 10, status="returned", returned_at=timezone.now())  # already back
    annotated = Member.objects.with_loan_counts().get(pk=member.pk)
    assert member.fees_due == annotated.fees_due == Decimal("3.00")  # 6 days x 0.50


def test_members_sorted_by_highest_bill():
    small, big = MemberFactory(full_name="Small"), MemberFactory(full_name="Big")
    MemberFactory(full_name="Nothing")
    late_loan(small, 1)
    late_loan(big, 9)
    assert [m.full_name for m in Member.objects.by_fees_due()] == ["Big", "Small"]


def test_card_numbers_are_unique_and_formatted():
    numbers = {MemberFactory().card_number for _ in range(20)}
    assert len(numbers) == 20
    assert all(n.startswith("LIB-") and len(n) == 12 for n in numbers)


# --- lookups -----------------------------------------------------------------


def test_top_fees_answer_links_to_the_member(admin_user):
    big = MemberFactory(full_name="Big Spender")
    late_loan(big, 9)
    result = lookups.run(admin_user, "top_fees", {"limit": 3})
    assert result.summary.startswith("Big Spender has the highest bill: €4.50")
    assert result.rows[0].url == reverse("admin:library_member_change", args=[big.pk])
    assert big.card_number in result.rows[0].detail


def test_find_member_by_card_number(admin_user):
    member = MemberFactory(full_name="Anna Card")
    result = lookups.run(admin_user, "find_member", {"query": member.card_number})
    assert "Anna Card" in result.summary and len(result.rows) == 1


def test_book_availability(admin_user):
    book = BookFactory(title="Dune", copies_total=2)
    LoanFactory(book=book)
    result = lookups.run(admin_user, "book_availability", {"query": "dune"})
    assert result.summary == "“Dune”: 1 of 2 on the shelf."


def test_overdue_and_summary(admin_user):
    late_loan(MemberFactory(), 3)
    assert "1 loans are overdue" in lookups.run(admin_user, "overdue_loans", {}).summary
    assert "1 of them overdue" in lookups.run(admin_user, "circulation_summary", {}).summary


@pytest.mark.parametrize(
    ("name", "params"),
    [
        ("drop_tables", {}),  # not in the allow-list
        ("find_member", {}),  # missing query
        ("find_member", {"query": "x"}),  # too short
        ("find_member", {"query": ["not", "a", "string"]}),
    ],
)
def test_bad_lookups_are_rejected(admin_user, name, params):
    with pytest.raises(lookups.LookupRejected):
        lookups.run(admin_user, name, params)


def test_limit_is_clamped(admin_user):
    for i in range(12):
        late_loan(MemberFactory(), i + 1)
    assert len(lookups.run(admin_user, "top_fees", {"limit": 500}).rows) == lookups.MAX_LIMIT
    assert len(lookups.run(admin_user, "top_fees", {"limit": "abc"}).rows) == 5


def test_lookups_respect_permissions(db):
    no_role = User.objects.create_user("norole", password="x", is_staff=True)
    assert lookups.allowed_lookups(no_role) == []
    with pytest.raises(lookups.LookupRejected):
        lookups.run(no_role, "top_fees", {})


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("who has the highest bill right now", "top_fees"),
        ("which members owe the most?", "top_fees"),
        ("how many books are overdue", "overdue_loans"),
        ("show me today's totals", "circulation_summary"),
        ("what does due date mean?", None),
        ("how do I fill in the ISBN?", None),
    ],
)
def test_keyword_router(question, expected):
    assert lookups.guess(question) == expected


# --- through the endpoint --------------------------------------------------------


def test_model_picks_lookup_and_server_answers_with_links(librarian_client, openrouter):
    member = MemberFactory(full_name="Deborah Thompson")
    late_loan(member, 7)
    openrouter.responses = [reply('{"lookup": "top_fees", "params": {"limit": 3}}')]
    data = ask(librarian_client, "who has the highest bill right now").json()
    assert data["lookup"] == "top_fees"
    assert data["answer"].startswith("Deborah Thompson has the highest bill: €3.50")
    assert data["results"][0]["url"] == reverse("admin:library_member_change", args=[member.pk])
    assert data["grounded"] is True and data["degraded"] is False


def test_library_data_is_never_sent_to_the_model(librarian_client, openrouter):
    member = MemberFactory(full_name="Private Person", email="private@example.org")
    late_loan(member, 5)
    openrouter.responses = [reply('{"lookup": "top_fees", "params": {}}')]
    ask(librarian_client, "who owes the most?")
    sent = json.dumps(openrouter.requests)
    assert "Private Person" not in sent
    assert "private@example.org" not in sent
    assert member.card_number not in sent


def test_prompt_lists_only_lookups_the_role_may_run(client, openrouter):
    user = User.objects.create_user("norole", password="x", is_staff=True)
    client.force_login(user)
    openrouter.responses = [reply('{"answer": "ok", "sources": []}')]
    ask(client, "what is this?")
    system = openrouter.requests[0]["messages"][0]["content"]
    assert "top_fees" not in system


def test_model_cannot_run_a_lookup_the_user_lacks(client, openrouter):
    user = User.objects.create_user("norole", password="x", is_staff=True)
    client.force_login(user)
    openrouter.responses = [reply('{"lookup": "top_fees", "params": {}}')]
    data = ask(client, "who owes the most?").json()
    assert data["results"] == []
    assert "can't look that up" in data["answer"]


def test_ai_down_still_answers_data_question_from_keywords(librarian_client, openrouter):
    late_loan(MemberFactory(full_name="Late Larry"), 2)
    openrouter.responses = [httpx.ConnectError("down"), httpx.ConnectError("down")]
    data = ask(librarian_client, "who has the highest bill?").json()
    assert data["degraded"] is True
    assert data["lookup"] == "top_fees"
    assert "Late Larry" in data["answer"]
