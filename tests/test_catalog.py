import pytest
from sqlalchemy import text

from app.main import app

HEADER = "sku,name,description,price_pkr,sizes,colors,stock,category\n"


def _signup(client, email):
    response = client.post(
        "/auth/signup",
        json={"seller_name": "Catalog", "email": email, "password": "correct-horse-battery"},
    )
    assert response.status_code == 201
    return client.get("/auth/csrf").json()["csrf_token"]


def _upload(client, token, body, filename="items.csv", content_type="text/csv"):
    payload = body.encode("utf-8") if isinstance(body, str) else body
    headers = {"X-CSRF-Token": token} if token else {}
    return client.post("/catalog/upload", headers=headers, files={"file": (filename, payload, content_type)})


def _valid(sku="a", price="100", **overrides):
    values = {
        "sku": sku,
        "name": "Neeli kurta",
        "description": "Kapray",
        "price_pkr": price,
        "sizes": "S|M",
        "colors": "Blue|White",
        "stock": "3",
        "category": "kapray",
    }
    values.update(overrides)
    return ",".join(values[key] for key in ("sku", "name", "description", "price_pkr", "sizes", "colors", "stock", "category")) + "\n"


def test_upload_upserts_preserves_absent_and_inactive_rows(client, conn):
    token = _signup(client, "catalog@example.com")
    first = _upload(client, token, HEADER + _valid("a") + _valid("b"))
    assert first.status_code == 200 and first.json() == {"created": 2, "updated": 0, "total": 2}
    conn.execute(text("UPDATE products SET is_active=false WHERE sku='b'"))
    second = _upload(client, token, HEADER + _valid("a", "150"))
    assert second.status_code == 200 and second.json() == {"created": 0, "updated": 1, "total": 1}
    assert conn.execute(text("SELECT sku,price_pkr,is_active FROM products ORDER BY sku")).all() == [("a", 150, True), ("b", 100, False)]


@pytest.mark.parametrize("filename,content_type,body,status", [
    ("items.txt", "text/csv", HEADER + _valid(), 415),
    ("items.csv", "application/octet-stream", HEADER + _valid(), 415),
    ("items.csv", "application/json", HEADER + _valid(), 415),
    ("items.csv", "text/csv", b"a\x00b", 415),
    ("items.csv", "text/csv", b"\xff", 415),
    ("items.csv", "text/csv", b"x" * (2 * 1024 * 1024 + 1), 413),
    ("items.csv", "text/csv", "sku,name\na,b\n", 422),
    ("items.csv", "text/csv", HEADER + "a,Neeli kurta,Kapray,100,S|M,Blue|White,3\n", 422),
    ("items.csv", "text/csv", HEADER + _valid(price="0"), 422),
    ("items.csv", "text/csv", HEADER + _valid(price="-5"), 422),
    ("items.csv", "text/csv", HEADER + _valid(price="12.5"), 422),
    ("items.csv", "text/csv", HEADER + _valid(price="abc"), 422),
    ("items.csv", "text/csv", HEADER + _valid(stock="-1"), 422),
    ("items.csv", "text/csv", HEADER + _valid(stock="2.5"), 422),
    ("items.csv", "text/csv", HEADER + _valid(stock="abc"), 422),
    ("items.csv", "text/csv", HEADER + _valid(sizes="|".join(str(x) for x in range(21))), 422),
    ("items.csv", "text/csv", HEADER + _valid(sizes="S||M"), 422),
    ("items.csv", "text/csv", HEADER + _valid(sizes="S|S"), 422),
    ("items.csv", "text/csv", HEADER + _valid(colors="Red|Red"), 422),
    ("items.csv", "text/csv", HEADER + _valid(sku="x" * 65), 422),
    ("items.csv", "text/csv", HEADER + _valid(sku="sku with space"), 422),
    ("items.csv", "text/csv", HEADER + _valid("same") + _valid("same"), 422),
    ("items.csv", "text/csv", HEADER + _valid(name="=SUM(12)"), 422),
    ("items.csv", "text/csv", HEADER + _valid(name="+cmd"), 422),
    ("items.csv", "text/csv", HEADER + _valid(name="-cmd"), 422),
    ("items.csv", "text/csv", HEADER + _valid(name="@cmd"), 422),
    ("items.csv", "text/csv", HEADER + _valid(name="\tcmd"), 422),
    ("items.csv", "text/csv", HEADER + _valid(name="\rcmd"), 422),
    ("items.csv", "text/csv", HEADER + _valid(description="=malicious"), 422),
    ("items.csv", "text/csv", HEADER + _valid(category="@category"), 422),
    ("items.csv", "text/csv", HEADER + _valid(sizes="=bad|M"), 422),
])
def test_invalid_upload_never_writes_rows(client, conn, filename, content_type, body, status):
    token = _signup(client, f"bad{abs(hash(str(body) + filename + content_type))}@example.com")
    response = _upload(client, token, body, filename, content_type)
    assert response.status_code == status, response.text
    assert conn.execute(text("SELECT count(*) FROM products")).scalar_one() == 0


def test_row_limit_header_match_and_bom(client, conn):
    token = _signup(client, "rows@example.com")
    assert _upload(client, token, HEADER + "".join(_valid(str(i)) for i in range(2001))).status_code == 422
    assert _upload(client, token, "name,sku,description,price_pkr,sizes,colors,stock,category\n").status_code == 422
    assert _upload(client, token, b"\xef\xbb\xbf" + (HEADER + _valid()).encode()).status_code == 200
    assert conn.execute(text("SELECT count(*) FROM products")).scalar_one() == 1


def test_invalid_report_has_file_line_column_count_and_cap(client, conn):
    token = _signup(client, "report@example.com")
    response = _upload(client, token, HEADER + _valid(price="0") + _valid("ok"))
    assert response.status_code == 422
    body = response.json()["detail"]
    assert body["total"] == 1 and body["errors"] == [{"row": 2, "column": "price_pkr", "reason": "Input should be greater than 0"}]
    assert conn.execute(text("SELECT count(*) FROM products")).scalar_one() == 0


def test_products_patch_filters_pagination_and_csrf(client):
    token = _signup(client, "manage@example.com")
    assert _upload(client, token, HEADER + _valid("a") + _valid("b")).status_code == 200
    a = client.get("/catalog/products").json()["items"][0]
    assert client.patch(f"/catalog/products/{a['id']}", json={"name": "X"}).status_code == 403
    assert client.patch(f"/catalog/products/{a['id']}", json={"sku": "x"}, headers={"X-CSRF-Token": token}).status_code == 422
    assert client.patch(f"/catalog/products/{a['id']}", json={"unknown": 1}, headers={"X-CSRF-Token": token}).status_code == 422
    assert client.post(f"/catalog/products/{a['id']}/deactivate", headers={"X-CSRF-Token": token}).status_code == 200
    assert len(client.get("/catalog/products?active=false").json()["items"]) == 1
    assert client.get("/catalog/products?active=true").json()["items"][0]["sku"] == "b"
    # Limit > 200 is rejected with 422 due to FastAPI Query(le=200) constraint
    assert client.get("/catalog/products?limit=201").status_code == 422
    assert client.get("/catalog/products?offset=1").json()["items"][0]["sku"] == "b"
    assert client.post(f"/catalog/products/{a['id']}/reactivate", headers={"X-CSRF-Token": token}).status_code == 200


def test_product_tenant_isolation_and_audit_redaction(client, conn):
    token_a = _signup(client, "a-catalog@example.com")
    _upload(client, token_a, HEADER + _valid("shared", name="Secret Product", description="Super Secret"))
    product = client.get("/catalog/products").json()["items"][0]
    client.cookies.clear()
    token_b = _signup(client, "b-catalog@example.com")
    assert client.get("/catalog/products").json()["items"] == []
    for method, path, kwargs in [
        (client.get, f"/catalog/products/{product['id']}", {}),
        (client.patch, f"/catalog/products/{product['id']}", {"json": {"name": "X"}, "headers": {"X-CSRF-Token": token_b}}),
        (client.post, f"/catalog/products/{product['id']}/deactivate", {"headers": {"X-CSRF-Token": token_b}}),
        (client.post, f"/catalog/products/{product['id']}/reactivate", {"headers": {"X-CSRF-Token": token_b}}),
    ]:
        assert method(path, **kwargs).status_code == 404
    assert _upload(client, token_b, HEADER + _valid("shared", name="Tenant B")).json()["created"] == 1
    audits = conn.execute(text("SELECT action,changes::text FROM audit_log WHERE entity_type='product'")).all()
    assert any(action == "csv_upload" for action, _ in audits)
    assert all("Secret Product" not in change and "Tenant B" not in change and "Super Secret" not in change for _, change in audits)


def test_all_protected_routes_require_session_and_mutations_require_csrf(client):
    public = {
        ("GET", "/"), ("GET", "/health"), ("POST", "/auth/signup"),
        ("POST", "/auth/login"), ("GET", "/webhooks/whatsapp"), ("POST", "/webhooks/whatsapp"),
    }
    for route in app.routes:
        if not getattr(route, "methods", None):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            if (method, route.path) not in public:
                target_path = (
                    route.path.replace("{product_id}", "00000000-0000-0000-0000-000000000000")
                    .replace("{faq_id}", "00000000-0000-0000-0000-000000000000")
                    .replace("{id}", "00000000-0000-0000-0000-000000000000")
                )
                response = getattr(client, method.lower())(target_path)
                assert response.status_code == 401, (method, route.path, response.status_code)
    token = _signup(client, "csrf-catalog@example.com")
    cases = [
        ("POST", "/catalog/upload", {"files": {"file": ("x.csv", HEADER.encode(), "text/csv")}}),
        ("POST", "/faqs", {"json": {"question": "Q", "answer": "A"}}),
    ]
    for method, path, kwargs in cases:
        assert getattr(client, method.lower())(path, **kwargs).status_code == 403
