from unittest import mock

from django.db.utils import OperationalError


def test_healthz_ok(client, db):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_reports_db_down_without_traceback(client, db):
    with mock.patch("config.views.connection.cursor", side_effect=OperationalError("down")):
        response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json() == {"status": "error", "db": "unreachable"}


def test_root_redirects_to_admin(client, db):
    response = client.get("/")
    assert response.status_code == 302
