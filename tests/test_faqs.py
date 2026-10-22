import pytest
from sqlalchemy import text


def _start(client, email):
    response = client.post(
        "/auth/signup",
        json={"seller_name": "FAQ", "email": email, "password": "correct-horse-battery"},
    )
    assert response.status_code == 201
    return client.get("/auth/csrf").json()["csrf_token"]


def test_faq_crud_pagination_csrf_and_validation(client, conn):
    csrf = _start(client, "faq@example.com")
    payload = {"question": " Delivery kab hogi? ", "answer": " Do din mein. "}
    assert client.post("/faqs", json=payload).status_code == 403
    created = client.post("/faqs", json=payload, headers={"X-CSRF-Token": csrf})
    assert created.status_code == 201
    faq_id = created.json()["id"]
    assert created.json()["question"] == "Delivery kab hogi?"
    assert created.json()["answer"] == "Do din mein."
    assert created.json()["active"] is True
    assert client.get("/faqs").json()["items"][0]["answer"] == "Do din mein."
    assert client.patch(f"/faqs/{faq_id}", json={"answer": "Kal."}).status_code == 403
    assert client.patch(f"/faqs/{faq_id}", json={"unknown": "x"}, headers={"X-CSRF-Token": csrf}).status_code == 422
    assert client.patch(f"/faqs/{faq_id}", json={"question": "  "}, headers={"X-CSRF-Token": csrf}).status_code == 422
    assert client.patch(f"/faqs/{faq_id}", json={}, headers={"X-CSRF-Token": csrf}).status_code == 422
    assert client.patch(f"/faqs/{faq_id}", json={"answer": "Updated answer"}, headers={"X-CSRF-Token": csrf}).status_code == 200
    # Limit > 200 is rejected with 422
    assert client.get("/faqs?limit=201").status_code == 422
    assert client.get("/faqs?offset=1").json()["items"] == []
    assert client.post(f"/faqs/{faq_id}/deactivate").status_code == 403
    assert client.post(f"/faqs/{faq_id}/deactivate", headers={"X-CSRF-Token": csrf}).json()["active"] is False
    assert client.post(f"/faqs/{faq_id}/reactivate", headers={"X-CSRF-Token": csrf}).json()["active"] is True
    rows = conn.execute(text("SELECT action, changes::text FROM audit_log WHERE entity_type='faq'")).all()
    assert {"create", "update"} <= {action for action, _ in rows}
    assert all("Delivery" not in changes and "Updated answer" not in changes for _, changes in rows)


def test_faq_cross_tenant_every_endpoint(client):
    csrf_a = _start(client, "faq-a@example.com")
    faq = client.post("/faqs", json={"question": "Question A", "answer": "Answer A"}, headers={"X-CSRF-Token": csrf_a}).json()
    client.cookies.clear()
    csrf_b = _start(client, "faq-b@example.com")
    assert client.get("/faqs").json()["items"] == []
    operations = [
        client.patch(f"/faqs/{faq['id']}", json={"question": "Q", "answer": "A"}, headers={"X-CSRF-Token": csrf_b}),
        client.post(f"/faqs/{faq['id']}/deactivate", headers={"X-CSRF-Token": csrf_b}),
        client.post(f"/faqs/{faq['id']}/reactivate", headers={"X-CSRF-Token": csrf_b}),
    ]
    assert [response.status_code for response in operations] == [404, 404, 404]


@pytest.mark.parametrize("payload", [
    {"question": "", "answer": "A"},
    {"question": " ", "answer": "A"},
    {"question": "Q", "answer": ""},
    {"question": "Q", "answer": " "},
    {"question": "Q" * 501, "answer": "A"},
    {"question": "Q", "answer": "A" * 2001},
])
def test_faq_create_rejects_invalid_lengths_or_empty_text(client, payload):
    csrf = _start(client, f"invalid{len(payload['question'])}{len(payload['answer'])}@example.com")
    assert client.post("/faqs", json=payload, headers={"X-CSRF-Token": csrf}).status_code == 422
