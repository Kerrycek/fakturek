from __future__ import annotations

from fastapi.testclient import TestClient

from fakturek.main import create_app
from fakturek.settings import get_settings


def test_public_invoice_url_is_not_forced_to_login(monkeypatch):
    # Ensure settings are reloaded with AUTH_REQUIRED=1
    get_settings.cache_clear()
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("AUTH_REQUIRED", "1")

    app = create_app()
    client = TestClient(app)

    # This path matches the public invoice pattern (/{username}/i/{token}/{invoice_number}).
    # Even with auth required, it must stay reachable without redirect.
    r = client.get("/acme/i/tok123/2026-0001", follow_redirects=False)

    assert r.status_code == 200
    assert "Databáze" in r.text or "DB" in r.text

    r_short = client.get("/i/demo-short-code", follow_redirects=False)
    assert r_short.status_code == 200
    assert "Databáze" in r_short.text or "DB" in r_short.text

    r_short_readable = client.get("/i/demo-short-code/2026-0001", follow_redirects=False)
    assert r_short_readable.status_code == 200
    assert "Databáze" in r_short_readable.text or "DB" in r_short_readable.text
