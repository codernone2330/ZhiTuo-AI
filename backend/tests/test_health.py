from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, base_url="http://127.0.0.1")


def test_root_returns_service_metadata() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["version"] == "0.1.0"


def test_live_health_has_trace_id() -> None:
    response = client.get("/api/v1/health/live")
    body = response.json()
    assert response.status_code == 200
    assert body["success"] is True
    assert body["data"]["status"] == "ok"
    assert body["traceId"] == response.headers["X-Trace-Id"]


def test_caller_trace_id_is_preserved() -> None:
    response = client.get("/api/v1/health/live", headers={"X-Trace-Id": "meeting-demo"})
    assert response.headers["X-Trace-Id"] == "meeting-demo"
    assert response.json()["traceId"] == "meeting-demo"
