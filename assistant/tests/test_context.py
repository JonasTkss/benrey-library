from django.test import override_settings

from assistant import context
from library.models import Book, Loan


def test_every_library_field_with_help_text_is_in_the_context():
    text = context.build_context()
    for model in (Book, Loan):
        for field in model._meta.get_fields():
            if field.concrete and not field.auto_created:
                assert f"{model._meta.model_name}.{field.name}" in text
                assert str(field.help_text) in text


def test_every_editable_field_has_help_text():
    """help_text is the grounding source, so a field without it is a bug."""
    missing = [f.id for m in context.get_models() for f in m.fields if not f.help_text]
    assert missing == []


def test_choices_and_defaults_are_included():
    text = context.build_context()
    assert "scifi=Science fiction" in text
    assert "default: 14 days from today" in text


def test_current_model_comes_first_and_field_is_marked():
    text = context.build_context("loan", "due_date")
    assert text.index("(model id: loan)") < text.index("(model id: book)")
    marked = text.split(">>> This is the field the user is looking at.")[0].rsplit("\n- ", 1)[1]
    assert marked.startswith("loan.due_date")


@override_settings(MAX_ACTIVE_LOANS=3)
def test_rules_follow_the_enforced_constants():
    rules = context.business_rules()
    assert "at most 3 books" in rules
    assert "14 days" in rules and "up to 7 days" in rules


def test_known_ids():
    assert "loan.due_date" in context.known_field_ids()
    assert "loan.id" not in context.known_field_ids()
    assert context.known_model_ids() == {"author", "book", "member", "loan"}
