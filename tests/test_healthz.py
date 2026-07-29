from fastapi.testclient import TestClient

from fakturek.main import create_app


def test_healthz_ok():
    client = TestClient(create_app())
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_home_ok():
    client = TestClient(create_app())
    res = client.get("/")
    assert res.status_code == 200
    assert "fakturek" in res.text.lower()
