"""Give every member a library card number.

Three steps, because a unique column with a random default cannot be added in
one go: Django would evaluate the default once and give every existing row the
same number. So: add the column as nullable, fill it row by row, then make it
required and unique.
"""

import secrets

from django.db import migrations, models

import library.models


def assign_card_numbers(apps, schema_editor):
    Member = apps.get_model("library", "Member")
    used = set()
    members = list(Member.objects.filter(card_number__isnull=True).only("pk"))
    for member in members:
        number = None
        while number is None or number in used:
            number = f"LIB-{secrets.randbelow(10**8):08d}"
        used.add(number)
        member.card_number = number
    Member.objects.bulk_update(members, ["card_number"], batch_size=500)


class Migration(migrations.Migration):
    dependencies = [
        ("library", "0002_role_groups"),
    ]

    operations = [
        migrations.AddField(
            model_name="member",
            name="card_number",
            field=models.CharField(max_length=12, null=True, editable=False),
        ),
        migrations.RunPython(assign_card_numbers, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="member",
            name="card_number",
            field=models.CharField(
                default=library.models.new_card_number,
                editable=False,
                help_text="Library card number (e.g. LIB-40217735), printed on the member's "
                "card. Assigned automatically; search by it to find a member at the desk.",
                max_length=12,
                unique=True,
            ),
        ),
    ]
