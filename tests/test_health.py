from fastapi.testclient import TestClient

from app.main import app, client_ip


def test_health_ok():
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "connected"
    # redis field is always present; value depends on whether REDIS_URL is set in the test env
    assert "redis" in body


def test_security_headers_present():
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.headers["Strict-Transport-Security"] == "max-age=63072000; includeSubDomains; preload"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_request_id_propagated():
    """The server must echo back the X-Request-ID we send, or generate one if absent."""
    with TestClient(app) as client:
        # Provided ID should be echoed back
        resp = client.get("/health", headers={"X-Request-ID": "my-trace-id-123"})
        assert resp.headers.get("x-request-id") == "my-trace-id-123"

        # No ID supplied: server must generate one (any non-empty string)
        resp2 = client.get("/health")
        generated = resp2.headers.get("x-request-id", "")
        assert len(generated) > 0


def test_trusted_proxy_forwarded_for():
    with TestClient(app) as client:
        # testclient is in default TRUSTED_PROXIES
        resp = client.get("/health", headers={"X-Forwarded-For": "203.0.113.195, 10.0.0.1"})
        assert resp.status_code == 200