import uuid

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

EMBEDDING_DIM = 1536

MSG_SQL = (
    "INSERT INTO messages (conversation_id, seller_id, direction, message_type, status, content, whatsapp_message_id) "
    "VALUES (:c, :s, :d, 'text', :st, 'hello', :w)"
)
ITEM_SQL = (
    "INSERT INTO order_items (order_id, seller_id, product_id, qty, price_pkr_at_order, size, color) "
    "VALUES (:o, :s, :p, :q, 100, :size, :color)"
)
EMB_SQL = (
    "INSERT INTO embeddings (seller_id, product_id, faq_id, content, content_hash, vector) "
    "VALUES (:s, :p, :f, 'c', :h, CAST(:v AS vector))"
)


def vec(n=EMBEDDING_DIM, value="0.1"):
    return "[" + ",".join([value] * n) + "]"


def _scalar(conn, sql, **params):
    return conn.execute(text(sql), params).scalar_one()


def new_seller(conn, name="seller"):
    return _scalar(conn, "INSERT INTO sellers (name) VALUES (:n) RETURNING id", n=name)


def new_conversation(conn, seller_id, phone="923001234567"):
    return _scalar(
        conn,
        "INSERT INTO conversations (seller_id, customer_phone) VALUES (:s, :p) RETURNING id",
        s=seller_id, p=phone,
    )


def new_product(conn, seller_id, sku=None, price=1000, stock=5):
    sku = sku or "sku-" + uuid.uuid4().hex[:8]
    return _scalar(
        conn,
        "INSERT INTO products (seller_id, sku, name, price_pkr, stock) "
        "VALUES (:s, :k, 'Test product', :pr, :st) RETURNING id",
        s=seller_id, k=sku, pr=price, st=stock,
    )


def new_faq(conn, seller_id):
    return _scalar(
        conn,
        "INSERT INTO faqs (seller_id, question, answer) VALUES (:s, 'q', 'a') RETURNING id",
        s=seller_id,
    )


def new_order(conn, seller_id, conversation_id, phone="923001234567"):
    return _scalar(
        conn,
        "INSERT INTO orders (seller_id, conversation_id, customer_name, customer_phone) "
        "VALUES (:s, :c, 'Ali', :p) RETURNING id",
        s=seller_id, c=conversation_id, p=phone,
    )


def add_message(conn, conversation_id, seller_id, d="inbound", st="received", w=None):
    conn.execute(text(MSG_SQL), dict(c=conversation_id, s=seller_id, d=d, st=st, w=w))


def add_item(conn, order_id, seller_id, product_id, q=1, size=None, color=None):
    conn.execute(text(ITEM_SQL), dict(o=order_id, s=seller_id, p=product_id, q=q, size=size, color=color))


def add_embedding(conn, seller_id, product_id=None, faq_id=None):
    conn.execute(text(EMB_SQL), dict(s=seller_id, p=product_id, f=faq_id, h="a" * 64, v=vec()))


def violation(conn, sql, **params):
    """Run `sql` inside a savepoint. It MUST fail. Returns (sqlstate, constraint_name)."""
    nested = conn.begin_nested()
    try:
        conn.execute(text(sql), params)
    except DBAPIError as exc:
        nested.rollback()
        orig = exc.orig
        code = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
        name = getattr(getattr(orig, "diag", None), "constraint_name", None)
        return code, name
    nested.rollback()
    raise AssertionError("expected a database error, but the statement succeeded")