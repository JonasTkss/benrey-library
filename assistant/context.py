"""Grounding for the assistant, built from the live model definitions.

Nothing here is a hand-written copy of the schema: labels, types, choices and
help_text are read from the Django fields at request time, so the assistant
can never describe a field that no longer exists or miss a new one. The only
hand-written part is BUSINESS_RULES, and it is built from the same constants
the code enforces.
"""

from dataclasses import dataclass
from functools import lru_cache

from django.apps import apps
from django.conf import settings
from django.db import models

from library import validators
from library.models import LATE_FEE_PER_DAY, LOAN_PERIOD_DAYS, MAX_EXTENSION_DAYS

APP_LABEL = "library"

FIELD_TYPES = {
    "CharField": "text",
    "TextField": "long text",
    "EmailField": "email address",
    "DateField": "date (YYYY-MM-DD)",
    "DateTimeField": "date and time",
    "BooleanField": "yes/no checkbox",
    "PositiveSmallIntegerField": "whole number",
    "ImageField": "image upload",
    "ForeignKey": "link to one {target}",
    "ManyToManyField": "links to one or more {target}",
}

# Defaults that are functions cannot be shown as a value; describe them.
CALLABLE_DEFAULTS = {
    "localdate": "today",
    "default_due_date": f"{LOAN_PERIOD_DAYS} days from today",
}


def business_rules() -> str:
    return "\n".join(
        [
            "- A loan can only be created if the book has a free copy and is not withdrawn.",
            f"- A member can have at most {settings.MAX_ACTIVE_LOANS} books on loan at once.",
            "- Suspended members cannot borrow. Only administrators can suspend or reactivate.",
            "- A member cannot borrow a second copy of a book they already have.",
            f"- The default loan period is {LOAN_PERIOD_DAYS} days.",
            f"- A loan can be extended once, by up to {MAX_EXTENSION_DAYS} days, and not when "
            "it is already overdue.",
            "- A loan is overdue when it is still on loan after its due date. Overdue is "
            "calculated, not a status you set.",
            f"- Late fee: EUR {LATE_FEE_PER_DAY} per day overdue.",
            "- 'Returned' is final; a returned loan cannot be reopened. Returns are done with "
            "the 'Mark selected loans as returned' action on the Loans list.",
            f"- Cover images: JPG, PNG or WebP, max {validators.COVER_MAX_BYTES // 1024 // 1024} "
            f"MB and {validators.COVER_MAX_PIXELS}x{validators.COVER_MAX_PIXELS} pixels.",
            "- Librarians can lend, return and extend loans and register members; only "
            "administrators can change books and authors or delete anything.",
        ]
    )


@dataclass(frozen=True)
class FieldInfo:
    id: str  # stable id, e.g. "loan.due_date"
    name: str
    label: str
    type: str
    required: bool
    help_text: str
    max_length: int | None
    choices: tuple[tuple[str, str], ...]
    unique: bool
    default: str | None

    def describe(self, *, focus: bool = False) -> str:
        parts = [self.id, f'label "{self.label}"', self.type]
        parts.append("required" if self.required else "optional")
        if self.unique:
            parts.append("must be unique")
        if self.max_length:
            parts.append(f"max {self.max_length} characters")
        if self.choices:
            parts.append("choices: " + ", ".join(f"{v}={label}" for v, label in self.choices))
        if self.default is not None:
            parts.append(f"default: {self.default}")
        line = "- " + " | ".join(parts) + f"\n  help: {self.help_text or '(none)'}"
        if focus:
            line += "\n  >>> This is the field the user is looking at."
        return line


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str
    fields: tuple[FieldInfo, ...]


def _default(field) -> str | None:
    if not field.has_default():
        return None
    default = field.default
    if callable(default):
        return CALLABLE_DEFAULTS.get(default.__name__, "set automatically")
    if field.choices:
        return dict(field.flatchoices).get(default, str(default))
    return str(default)


def _field_info(model_id: str, field) -> FieldInfo:
    internal = field.get_internal_type()
    target = field.related_model._meta.verbose_name if field.is_relation else ""
    auto = getattr(field, "auto_now_add", False) or getattr(field, "auto_now", False)
    return FieldInfo(
        id=f"{model_id}.{field.name}",
        name=field.name,
        label=str(field.verbose_name),
        type=FIELD_TYPES.get(internal, internal).format(target=target),
        required=not field.blank and not auto,
        help_text=str(field.help_text),
        max_length=field.max_length if internal in {"CharField", "EmailField"} else None,
        choices=tuple((str(v), str(label)) for v, label in field.flatchoices),
        unique=field.unique and not field.primary_key,
        default=_default(field) if not auto else "set automatically",
    )


@lru_cache(maxsize=1)  # model definitions do not change while the process runs
def get_models() -> tuple[ModelInfo, ...]:
    infos = []
    for model in apps.get_app_config(APP_LABEL).get_models():
        model_id = model._meta.model_name
        fields = tuple(
            _field_info(model_id, field)
            for field in model._meta.get_fields()
            # Only real, editable columns; skip ids and reverse relations.
            if field.concrete and not field.auto_created and not isinstance(field, models.AutoField)
        )
        infos.append(ModelInfo(model_id, str(model._meta.verbose_name), fields))
    return tuple(infos)


def known_field_ids() -> set[str]:
    return {f.id for m in get_models() for f in m.fields}


def known_model_ids() -> set[str]:
    return {m.id for m in get_models()}


def field_by_id(field_id: str) -> FieldInfo | None:
    return next((f for m in get_models() for f in m.fields if f.id == field_id), None)


def build_context(model_id: str | None = None, field_name: str | None = None) -> str:
    """Field definitions + rules, with the page the user is on listed first."""
    models_ = sorted(get_models(), key=lambda m: m.id != model_id)
    sections = []
    for info in models_:
        on_page = info.id == model_id
        title = f"## {info.label.title()} (model id: {info.id})"
        if on_page:
            title += " - the form the user has open"
        lines = [f.describe(focus=on_page and f.name == field_name) for f in info.fields]
        sections.append(title + "\n" + "\n".join(lines))
    return "# Field definitions\n\n" + "\n\n".join(sections) + "\n\n# Rules\n" + business_rules()
