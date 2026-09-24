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


def test_frontend_is_served_by_the_backend() -> None:
    response = client.get("/app/")
    assert response.status_code == 200
    assert "智拓 · 商机作战助手" in response.text
    assert "loginWithBackend" in response.text
    assert 'location.pathname.indexOf("/app/")===0?location.origin' in response.text

    logo = client.get("/app/china-mobile-logo.svg")
    assert logo.status_code == 200
    assert b"<svg" in logo.content


def test_openapi_registers_unified_map_routes() -> None:
    paths = app.openapi()["paths"]
    assert "/api/v1/maps/status" in paths
    assert "/api/v1/maps/visit-route" in paths


def test_openapi_registers_customer_read_chain() -> None:
    paths = app.openapi()["paths"]
    assert "/api/v1/customers" in paths
    assert "/api/v1/customers/import" in paths
    assert "/api/v1/customers/{customer_ref}" in paths


def test_openapi_registers_frontend_workflows() -> None:
    paths = app.openapi()["paths"]
    assert {
        "/api/v1/auth/login",
        "/api/v1/organizations/tree",
        "/api/v1/customers",
        "/api/v1/visits",
        "/api/v1/opportunities/refresh",
        "/api/v1/maps/visit-route",
        "/api/v1/ai/leads/capture",
        "/api/v1/ai/chat",
        "/api/v1/documents",
        "/api/v1/reports/weekly",
    }.issubset(paths)
