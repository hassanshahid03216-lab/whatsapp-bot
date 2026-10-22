import pytest
from sqlalchemy import text

from tests.helpers import (
    EMB_SQL, ITEM_SQL, MSG_SQL, add_embedding, add_item, add_message,
    new_conversation, new_faq, new_order, new_product, new_seller, vec, violation,
)

FK, UNIQUE, CHECK = "23503", "23505", "23514"

EXPECTED_TABLES = {
    "sellers", "users", "products", "faqs", "conversations", "messages",
    "orders", "order_items", "embeddings", "audit_log",
}


# ---------- schema basics ----------

def test_all_tables_exist(conn):
    rows = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars().all()
    assert EXPECTED_TABLES <= set(rows)


def test_vector_extension_installed(conn):
    n = conn.execute(text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")).scalar_one()
    assert n == 1


# ---------- tenant isolation (DB level) ----------

def test_same_tenant_inserts_work(conn):
    """Positive control: if this fails, the isolation tests below prove nothing."""
    s = new_seller(conn)
    c = new_conversation(conn, s)
    p = new_product(conn, s)
    f = new_faq(conn, s)
    o = new_order(conn, s, c)
    add_message(conn, c, s)
    add_item(conn, o, s, p)
    add_embedding(conn, s, product_id=p)
    add_embedding(conn, s, faq_id=f)


def test_message_cannot_use_other_sellers_conversation(conn):
    a, b = new_seller(conn, "a"), new_seller(conn, "b")
    conv_a = new_conversation(conn, a)
    assert violation(conn, MSG_SQL, c=conv_a, s=b, d="inbound", st="received", w=None) == (
        FK, "fk_messages_conversation_seller")


def test_order_cannot_use_other_sellers_conversation(conn):
    a, b = new_seller(conn, "a"), new_seller(conn, "b")
    conv_a = new_conversation(conn, a)
    code, name = violation(
        conn,
        "INSERT INTO orders (seller_id, conversation_id, customer_name, customer_phone) "
        "VALUES (:s, :c, 'Ali', '923001234567')",
        s=b, c=conv_a,
    )
    assert (code, name) == (FK, "fk_orders_conversation_seller")


def test_order_item_cannot_use_other_sellers_order(conn):
    a, b = new_seller(conn, "a"), new_seller(conn, "b")
    order_a = new_order(conn, a, new_conversation(conn, a))
    product_b = new_product(conn, b)
    assert violation(conn, ITEM_SQL, o=order_a, s=b, p=product_b, q=1, size=None, color=None) == (
        FK, "fk_order_items_order_seller")


def test_order_item_cannot_use_other_sellers_product(conn):
    a, b = new_seller(conn, "a"), new_seller(conn, "b")
    order_b = new_order(conn, b, new_conversation(conn, b))
    product_a = new_product(conn, a)
    assert violation(conn, ITEM_SQL, o=order_b, s=b, p=product_a, q=1, size=None, color=None) == (
        FK, "fk_order_items_product_seller")


def test_embedding_cannot_use_other_sellers_product(conn):
    a, b = new_seller(conn, "a"), new_seller(conn, "b")
    product_a = new_product(conn, a)
    assert violation(conn, EMB_SQL, s=b, p=product_a, f=None, h="a" * 64, v=vec()) == (
        FK, "fk_embeddings_product_seller")


def test_embedding_cannot_use_other_sellers_faq(conn):
    a, b = new_seller(conn, "a"), new_seller(conn, "b")
    faq_a = new_faq(conn, a)
    assert violation(conn, EMB_SQL, s=b, p=None, f=faq_a, h="a" * 64, v=vec()) == (
        FK, "fk_embeddings_faq_seller")


# ---------- conversations ----------

@pytest.mark.parametrize("bad", ["+923001234567", "03001234567", "12345", "0923001234567", "9230012345678901", "92300abc4567"])
def test_conversation_phone_check_rejects_bad_numbers(conn, bad):
    s = new_seller(conn)
    assert violation(
        conn, "INSERT INTO conversations (seller_id, customer_phone) VALUES (:s, :p)", s=s, p=bad
    ) == (CHECK, "chk_conversations_phone")


def test_conversation_same_phone_twice_for_one_seller_rejected(conn):
    s = new_seller(conn)
    new_conversation(conn, s)
    assert violation(
        conn, "INSERT INTO conversations (seller_id, customer_phone) VALUES (:s, '923001234567')", s=s
    ) == (UNIQUE, "uq_conversations_seller_phone")


def test_same_phone_allowed_for_different_sellers(conn):
    new_conversation(conn, new_seller(conn, "a"))
    new_conversation(conn, new_seller(conn, "b"))


# ---------- messages ----------

def test_duplicate_whatsapp_message_id_rejected(conn):
    s = new_seller(conn)
    c = new_conversation(conn, s)
    add_message(conn, c, s, w="wamid.AAA")
    assert violation(conn, MSG_SQL, c=c, s=s, d="inbound", st="received", w="wamid.AAA") == (
        UNIQUE, "uq_messages_whatsapp_message_id")


def test_many_messages_per_conversation_allowed(conn):
    """Regression: an old model allowed only one inbound + one outbound per conversation."""
    s = new_seller(conn)
    c = new_conversation(conn, s)
    for i in range(5):
        add_message(conn, c, s, d="inbound", w=f"wamid.in{i}")
        add_message(conn, c, s, d="outbound", st="queued", w=None)  # NULL ids may repeat


def test_message_status_and_direction_checks(conn):
    s = new_seller(conn)
    c = new_conversation(conn, s)
    assert violation(conn, MSG_SQL, c=c, s=s, d="inbound", st="bogus", w=None) == (CHECK, "chk_messages_status")
    assert violation(conn, MSG_SQL, c=c, s=s, d="sideways", st="received", w=None) == (CHECK, "chk_messages_direction")


# ---------- orders / order_items ----------

def test_order_status_check(conn):
    s = new_seller(conn)
    c = new_conversation(conn, s)
    assert violation(
        conn,
        "INSERT INTO orders (seller_id, conversation_id, customer_name, customer_phone, status) "
        "VALUES (:s, :c, 'Ali', '923001234567', 'shipped')",
        s=s, c=c,
    ) == (CHECK, "chk_orders_status")


def test_order_phone_check(conn):
    s = new_seller(conn)
    c = new_conversation(conn, s)
    assert violation(
        conn,
        "INSERT INTO orders (seller_id, conversation_id, customer_name, customer_phone) "
        "VALUES (:s, :c, 'Ali', '+923001234567')",
        s=s, c=c,
    ) == (CHECK, "chk_orders_phone")


def test_order_items_nulls_not_distinct(conn):
    s = new_seller(conn)
    o = new_order(conn, s, new_conversation(conn, s))
    p = new_product(conn, s)
    add_item(conn, o, s, p)  # size NULL, color NULL
    assert violation(conn, ITEM_SQL, o=o, s=s, p=p, q=1, size=None, color=None) == (
        UNIQUE, "uq_order_items_order_product_size_color")
    add_item(conn, o, s, p, size="M")  # a different size is a different line
    add_item(conn, o, s, p, size="M", color="red")


def test_order_item_qty_must_be_positive(conn):
    s = new_seller(conn)
    o = new_order(conn, s, new_conversation(conn, s))
    p = new_product(conn, s)
    assert violation(conn, ITEM_SQL, o=o, s=s, p=p, q=0, size=None, color=None) == (
        CHECK, "chk_order_items_qty_positive")


def test_product_with_orders_cannot_be_hard_deleted(conn):
    s = new_seller(conn)
    o = new_order(conn, s, new_conversation(conn, s))
    p = new_product(conn, s)
    add_item(conn, o, s, p)
    assert violation(conn, "DELETE FROM products WHERE id = :p", p=p) == (FK, "fk_order_items_product_seller")


# ---------- products ----------

def test_product_checks(conn):
    s = new_seller(conn)
    base = "INSERT INTO products (seller_id, sku, name, price_pkr, stock) VALUES (:s, 'k1', 'n', :pr, :st)"
    assert violation(conn, base, s=s, pr=0, st=1) == (CHECK, "chk_products_price_positive")
    assert violation(conn, base, s=s, pr=100, st=-1) == (CHECK, "chk_products_stock_nonnegative")
    assert violation(
        conn,
        "INSERT INTO products (seller_id, sku, name, price_pkr, sizes) "
        "VALUES (:s, 'k2', 'n', 100, CAST(:z AS text[]))",
        s=s, z=[f"s{i}" for i in range(21)],
    ) == (CHECK, "chk_products_sizes_len")


def test_duplicate_sku_same_seller_rejected_but_ok_across_sellers(conn):
    a, b = new_seller(conn, "a"), new_seller(conn, "b")
    new_product(conn, a, sku="SKU-1")
    new_product(conn, b, sku="SKU-1")
    assert violation(
        conn,
        "INSERT INTO products (seller_id, sku, name, price_pkr) VALUES (:s, 'SKU-1', 'n', 100)",
        s=a,
    ) == (UNIQUE, "uq_products_seller_sku")


# ---------- embeddings ----------

def test_embedding_needs_exactly_one_source(conn):
    s = new_seller(conn)
    p = new_product(conn, s)
    f = new_faq(conn, s)
    assert violation(conn, EMB_SQL, s=s, p=None, f=None, h="a" * 64, v=vec()) == (
        CHECK, "chk_embeddings_exactly_one_source")
    assert violation(conn, EMB_SQL, s=s, p=p, f=f, h="a" * 64, v=vec()) == (
        CHECK, "chk_embeddings_exactly_one_source")


def test_embedding_wrong_dimension_rejected(conn):
    s = new_seller(conn)
    p = new_product(conn, s)
    code, _ = violation(conn, EMB_SQL, s=s, p=p, f=None, h="a" * 64, v=vec(3))
    assert code.startswith("22")  # data exception: expected 1536 dimensions


def test_one_embedding_per_product(conn):
    s = new_seller(conn)
    p = new_product(conn, s)
    add_embedding(conn, s, product_id=p)
    assert violation(conn, EMB_SQL, s=s, p=p, f=None, h="a" * 64, v=vec()) == (
        UNIQUE, "uq_embeddings_seller_product")


def test_deleting_product_or_faq_deletes_its_embedding(conn):
    s = new_seller(conn)
    p = new_product(conn, s)
    f = new_faq(conn, s)
    add_embedding(conn, s, product_id=p)
    add_embedding(conn, s, faq_id=f)
    conn.execute(text("DELETE FROM products WHERE id = :p"), {"p": p})
    conn.execute(text("DELETE FROM faqs WHERE id = :f"), {"f": f})
    left = conn.execute(text("SELECT count(*) FROM embeddings WHERE seller_id = :s"), {"s": s}).scalar_one()
    assert left == 0


# ---------- users / sellers / audit ----------

USER_SQL = "INSERT INTO users (seller_id, email, password_hash) VALUES (:s, :e, 'x')"


def test_user_email_must_be_lowercase(conn):
    s = new_seller(conn)
    assert violation(conn, USER_SQL, s=s, e="Ali@Example.com") == (CHECK, "chk_users_email_lower")


def test_user_email_unique_across_sellers(conn):
    a, b = new_seller(conn, "a"), new_seller(conn, "b")
    conn.execute(text(USER_SQL), {"s": a, "e": "ali@example.com"})
    assert violation(conn, USER_SQL, s=b, e="ali@example.com") == (UNIQUE, "uq_users_lower_email")


def test_one_user_per_seller(conn):
    s = new_seller(conn)
    conn.execute(text(USER_SQL), {"s": s, "e": "one@example.com"})
    assert violation(conn, USER_SQL, s=s, e="two@example.com") == (UNIQUE, "uq_users_seller_id")


def test_whatsapp_phone_number_id_unique_but_nullable(conn):
    conn.execute(text("INSERT INTO sellers (name) VALUES ('a')"))
    conn.execute(text("INSERT INTO sellers (name) VALUES ('b')"))  # two NULLs are fine
    conn.execute(text("INSERT INTO sellers (name, whatsapp_phone_number_id) VALUES ('c', '1001')"))
    assert violation(
        conn, "INSERT INTO sellers (name, whatsapp_phone_number_id) VALUES ('d', '1001')"
    ) == (UNIQUE, "uq_sellers_whatsapp_phone_number_id")


def test_audit_log_checks_and_nullable_entity(conn):
    s = new_seller(conn)
    conn.execute(
        text("INSERT INTO audit_log (seller_id, action, entity_type) VALUES (:s, 'login', 'user')"), {"s": s}
    )
    assert violation(
        conn, "INSERT INTO audit_log (seller_id, action, entity_type) VALUES (:s, 'hack', 'user')", s=s
    ) == (CHECK, "chk_audit_action")