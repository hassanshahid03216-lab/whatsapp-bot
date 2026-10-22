import hashlib
import hmac
import json
import pytest

from cryptography.fernet import Fernet
from sqlalchemy import text

from app.whatsapp import encrypt_access_token


def test_dashboard_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Seller dashboard" in response.text


def test_webhook_verification_challenge(client, monkeypatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "test-verify-token")
    response = client.get("/webhooks/whatsapp", params={
        "hub.mode": "subscribe",
        "hub.verify_token": "test-verify-token",
        "hub.challenge": "challenge-123",
    })
    assert response.status_code == 200
    assert response.text == "challenge-123"
    assert client.get("/webhooks/whatsapp", params={
        "hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "x",
    }).status_code == 403


def test_webhook_rejects_invalid_signature(client, monkeypatch):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "app-secret")
    response = client.post("/webhooks/whatsapp", content=b"{}", headers={"X-Hub-Signature-256": "sha256=bad"})
    assert response.status_code == 401


def test_signed_inbound_message_is_saved_and_replied(client, conn, monkeypatch):
    from app import whatsapp

    app_secret = "local-test-app-secret"
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", app_secret)
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN_KEY", key)
    seller_id = conn.execute(
        text("INSERT INTO sellers (name, whatsapp_phone_number_id, whatsapp_access_token_encrypted) "
             "VALUES ('webhook seller', 'phone-id-test', :token) RETURNING id"),
        {"token": encrypt_access_token("local-test-access-token")},
    ).scalar_one()
    sent = []

    def fake_send(phone_number_id, access_token, recipient, message):
        sent.append((phone_number_id, access_token, recipient, message))
        return "wamid.out.123"

    monkeypatch.setattr(whatsapp, "send_text_message", fake_send)
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": "phone-id-test"},
            "contacts": [{"wa_id": "923001234567", "profile": {"name": "Customer"}}],
            "messages": [{"from": "923001234567", "id": "wamid.in.123", "type": "text",
                          "text": {"body": "hello"}}],
        }}]}],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()
    response = client.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": signature})

    assert response.status_code == 200
    assert len(sent) == 1
    assert sent[0][:3] == ("phone-id-test", "local-test-access-token", "923001234567")
    assert "could not find a confident match" in sent[0][3].lower()
    assert conn.execute(text("SELECT count(*) FROM conversations WHERE seller_id=:s"), {"s": seller_id}).scalar_one() == 1
    messages = conn.execute(text(
        "SELECT direction, status, whatsapp_message_id FROM messages "
        "WHERE seller_id=:s ORDER BY created_at, direction"
    ), {"s": seller_id}).all()
    assert {row.whatsapp_message_id for row in messages} == {"wamid.in.123", "wamid.out.123"}
    assert {row.direction for row in messages} == {"inbound", "outbound"}


def test_whatsapp_settings_encrypt_access_token(client, monkeypatch, conn):
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN_KEY", key)
    signup = client.post("/auth/signup", json={
        "seller_name": "Settings shop", "email": "settings@example.com", "password": "unique-password-123",
    })
    assert signup.status_code == 201
    csrf = client.get("/auth/csrf").json()["csrf_token"]
    secret = "test-token-value-that-must-not-be-returned"
    response = client.put("/settings/whatsapp", json={
        "phone_number_id": "phone-settings-id", "access_token": secret,
    }, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert secret not in response.text
    stored = conn.execute(text(
        "SELECT whatsapp_access_token_encrypted FROM sellers WHERE id=:seller_id"
    ), {"seller_id": signup.json()["seller_id"]}).scalar_one()
    assert stored != secret
    assert client.get("/settings/whatsapp").json() == {
        "connected": True, "phone_number_id": "phone-settings-id",
    }


def test_whatsapp_status_updates(client, conn, monkeypatch):
    app_secret = "status-test-secret"
    monkeypatch.setenv("WHATSAPP_APP_SECRET", app_secret)

    seller_id = conn.execute(
        text("INSERT INTO sellers (name, whatsapp_phone_number_id) VALUES ('status seller', 'phone-status-test') RETURNING id")
    ).scalar_one()

    conv_id = conn.execute(
        text("INSERT INTO conversations (seller_id, customer_phone) VALUES (:s, '923000000001') RETURNING id"),
        {"s": seller_id},
    ).scalar_one()

    conn.execute(
        text("INSERT INTO messages (conversation_id, seller_id, whatsapp_message_id, direction, message_type, status, content) "
             "VALUES (:c, :s, 'wamid.out.status1', 'outbound', 'text', 'sent', 'Test reply')"),
        {"c": conv_id, "s": seller_id},
    )

    # 1. Test Delivered Status
    payload_delivered = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": "phone-status-test"},
            "statuses": [{"id": "wamid.out.status1", "status": "delivered", "timestamp": "1710000000"}],
        }}]}],
    }
    raw = json.dumps(payload_delivered).encode()
    sig = "sha256=" + hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()
    res = client.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": sig})
    assert res.status_code == 200

    row = conn.execute(text("SELECT status, delivered_at FROM messages WHERE whatsapp_message_id='wamid.out.status1'")).one()
    assert row.status == "delivered"
    assert row.delivered_at is not None

    # 2. Test Read Status
    payload_read = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": "phone-status-test"},
            "statuses": [{"id": "wamid.out.status1", "status": "read", "timestamp": "1710000050"}],
        }}]}],
    }
    raw = json.dumps(payload_read).encode()
    sig = "sha256=" + hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()
    res = client.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": sig})
    assert res.status_code == 200

    row = conn.execute(text("SELECT status, read_at FROM messages WHERE whatsapp_message_id='wamid.out.status1'")).one()
    assert row.status == "read"
    assert row.read_at is not None

    # 3. Test Failed Status
    payload_failed = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": "phone-status-test"},
            "statuses": [{
                "id": "wamid.out.status1",
                "status": "failed",
                "errors": [{"code": 131026, "title": "Undeliverable"}],
            }],
        }}]}],
    }
    raw = json.dumps(payload_failed).encode()
    sig = "sha256=" + hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()
    res = client.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": sig})
    assert res.status_code == 200

    row = conn.execute(text("SELECT status FROM messages WHERE whatsapp_message_id='wamid.out.status1'")).one()
    assert row.status == "failed"
    conv = conn.execute(text("SELECT needs_seller_action, handoff_reason FROM conversations WHERE id=:c"), {"c": conv_id}).one()
    assert conv.needs_seller_action is True
    assert "failed" in conv.handoff_reason.lower()


def test_incoming_media_message_handling(client, conn, monkeypatch):
    from app import whatsapp

    app_secret = "media-test-app-secret"
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", app_secret)
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN_KEY", key)

    seller_id = conn.execute(
        text("INSERT INTO sellers (name, whatsapp_phone_number_id, whatsapp_access_token_encrypted) "
             "VALUES ('media seller', 'phone-media-test', :token) RETURNING id"),
        {"token": encrypt_access_token("media-access-token")},
    ).scalar_one()

    sent = []

    def fake_send(phone_number_id, access_token, recipient, message):
        sent.append((phone_number_id, access_token, recipient, message))
        return "wamid.out.media_ack"

    monkeypatch.setattr(whatsapp, "send_text_message", fake_send)

    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": "phone-media-test"},
            "contacts": [{"wa_id": "923009999999", "profile": {"name": "MediaCustomer"}}],
            "messages": [{
                "from": "923009999999",
                "id": "wamid.in.media1",
                "type": "image",
                "image": {"id": "media-id-123", "caption": "Is this in stock?"},
            }],
        }}]}],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()
    response = client.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": signature})

    assert response.status_code == 200
    assert len(sent) == 1
    assert "image" in sent[0][3].lower() or "review" in sent[0][3].lower()

    conv = conn.execute(
        text("SELECT needs_seller_action, handoff_reason FROM conversations WHERE seller_id=:s"),
        {"s": seller_id},
    ).one()
    assert conv.needs_seller_action is True
    assert "image" in conv.handoff_reason.lower()

    inbound = conn.execute(
        text("SELECT content, status FROM messages WHERE whatsapp_message_id='wamid.in.media1'")
    ).one()
    assert "[Image]" in inbound.content
    assert "Is this in stock?" in inbound.content


def test_conversation_stream_endpoint(client, db_session):
    # Unauthenticated should return 401
    fake_id = "00000000-0000-0000-0000-000000000001"
    assert client.get(f"/api/conversations/{fake_id}/stream").status_code == 401

    # Sign up seller and create conversation
    signup = client.post("/auth/signup", json={
        "seller_name": "Stream Shop", "email": "stream@example.com", "password": "unique-password-stream",
    })
    assert signup.status_code == 201
    seller_id = signup.json()["seller_id"]

    conv = Conversations(seller_id=uuid.UUID(seller_id), customer_phone="923007777777")
    db_session.add(conv)
    db_session.flush()
    conv_id = conv.id

    # Authenticated seller stream endpoint returns text/event-stream
    response = client.get(f"/api/conversations/{conv_id}/stream?limit_one=1")
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    assert "connected" in response.text
    assert str(conv_id) in response.text
