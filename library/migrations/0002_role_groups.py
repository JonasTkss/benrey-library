"""Create the two staff roles as auth Groups, in every environment.

Roles are plain Django permissions, so the admin enforces them itself: a
missing add/delete permission removes the button, the action and the URL
(403), and a view-only permission renders the form read-only.

Edit ROLE_PERMISSIONS here (and in a new migration) to change a role; the test
in library/tests/test_roles.py pins the exact sets.
"""

from django.contrib.auth.management import create_permissions
from django.db import migrations

LIBRARY_MODELS = ["author", "book", "member", "loan"]
CRUD = ["add", "change", "delete", "view"]

ROLE_PERMISSIONS = {
    # Owns the catalogue, the members and the staff accounts.
    "Admin": [f"library.{action}_{model}" for model in LIBRARY_MODELS for action in CRUD]
    + ["library.change_member_status"]
    + [f"auth.{action}_{model}" for model in ("user", "group") for action in CRUD]
    + ["axes.view_accessattempt", "axes.view_accessfailurelog", "axes.delete_accessattempt"],
    # Runs the front desk: lends and takes back books, registers members.
    # Reads the catalogue but cannot change it, and cannot delete anything.
    "Librarian": [
        "library.view_author",
        "library.view_book",
        "library.add_member",
        "library.change_member",
        "library.view_member",
        "library.add_loan",
        "library.change_loan",
        "library.view_loan",
    ],
}


def create_role_groups(apps, schema_editor):
    # Permissions are normally created after all migrations finish
    # (post_migrate). Create them now so this migration can assign them on a
    # fresh database.
    for app_config in apps.get_app_configs():
        app_config.models_module = True
        create_permissions(app_config, verbosity=0)
        app_config.models_module = None

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    for role, codes in ROLE_PERMISSIONS.items():
        group, _ = Group.objects.get_or_create(name=role)
        perms = []
        for code in codes:
            app_label, codename = code.split(".")
            perms.append(
                Permission.objects.get(content_type__app_label=app_label, codename=codename)
            )
        group.permissions.set(perms)


def remove_role_groups(apps, schema_editor):
    apps.get_model("auth", "Group").objects.filter(name__in=ROLE_PERMISSIONS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("library", "0001_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("axes", "0009_add_session_hash"),
    ]

    operations = [migrations.RunPython(create_role_groups, remove_role_groups)]
