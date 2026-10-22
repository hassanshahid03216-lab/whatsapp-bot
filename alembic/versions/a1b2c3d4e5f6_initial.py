"""initial schema

Revision ID: a1b2c3d4e5f6
Revises:
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector

revision = "a1b2c3d4e5f6"
down_revision = None
branch_labels = None
depends_on = None

EMBEDDING_DIM = 1536
UUID = postgresql.UUID(as_uuid=True)
PHONE_CHECK = "customer_phone ~ '^[1-9][0-9]{7,14}$'"


def _id():
    return sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _created():
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))


def _updated():
    return sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))


def _bool(name, default):
    return sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.text(default))


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "sellers",
        _id(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("whatsapp_phone_number_id", sa.Text(), nullable=True),
        sa.Column("whatsapp_access_token_encrypted", sa.Text(), nullable=True),
        _bool("is_active", "true"),
        _created(),
        _updated(),
        sa.UniqueConstraint("whatsapp_phone_number_id", name="uq_sellers_whatsapp_phone_number_id"),
    )

    op.create_table(
        "users",
        _id(),
        sa.Column("seller_id", UUID, sa.ForeignKey("sellers.id"), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        _bool("is_active", "true"),
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
        _created(),
        _updated(),
        sa.UniqueConstraint("seller_id", name="uq_users_seller_id"),
        sa.CheckConstraint("email = lower(email)", name="chk_users_email_lower"),
    )
    op.create_index("uq_users_lower_email", "users", [sa.text("lower(email)")], unique=True)

    op.create_table(
        "products",
        _id(),
        sa.Column("seller_id", UUID, sa.ForeignKey("sellers.id"), nullable=False),
        sa.Column("sku", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price_pkr", sa.Integer(), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("sizes", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("colors", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("stock", sa.Integer(), nullable=False, server_default=sa.text("0")),
        _bool("is_active", "true"),
        _created(),
        _updated(),
        sa.UniqueConstraint("seller_id", "sku", name="uq_products_seller_sku"),
        sa.UniqueConstraint("id", "seller_id", name="uq_products_id_seller_id"),
        sa.CheckConstraint("price_pkr > 0", name="chk_products_price_positive"),
        sa.CheckConstraint("stock >= 0", name="chk_products_stock_nonnegative"),
        sa.CheckConstraint("cardinality(sizes) <= 20", name="chk_products_sizes_len"),
        sa.CheckConstraint("cardinality(colors) <= 20", name="chk_products_colors_len"),
    )

    op.create_table(
        "faqs",
        _id(),
        sa.Column("seller_id", UUID, sa.ForeignKey("sellers.id"), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        _bool("is_active", "true"),
        _created(),
        _updated(),
        sa.UniqueConstraint("id", "seller_id", name="uq_faqs_id_seller_id"),
    )
    op.create_index("ix_faqs_seller_id", "faqs", ["seller_id"])

    op.create_table(
        "conversations",
        _id(),
        sa.Column("seller_id", UUID, sa.ForeignKey("sellers.id"), nullable=False),
        sa.Column("customer_phone", sa.Text(), nullable=False),
        sa.Column("customer_name", sa.Text(), nullable=True),
        sa.Column("last_customer_message_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        _bool("bot_active", "true"),
        _bool("needs_seller_action", "false"),
        sa.Column("handoff_reason", sa.Text(), nullable=True),
        _created(),
        _updated(),
        sa.UniqueConstraint("seller_id", "customer_phone", name="uq_conversations_seller_phone"),
        sa.UniqueConstraint("id", "seller_id", name="uq_conversations_id_seller_id"),
        sa.CheckConstraint(PHONE_CHECK, name="chk_conversations_phone"),
    )
    op.create_index("ix_conversations_seller_last_msg", "conversations", ["seller_id", "last_customer_message_at"])

    op.create_table(
        "messages",
        _id(),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("seller_id", UUID, nullable=False),
        sa.Column("whatsapp_message_id", sa.Text(), nullable=True),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("message_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        _created(),
        _updated(),
        sa.UniqueConstraint("whatsapp_message_id", name="uq_messages_whatsapp_message_id"),
        sa.CheckConstraint("direction IN ('inbound','outbound')", name="chk_messages_direction"),
        sa.CheckConstraint("message_type = 'text'", name="chk_messages_type"),
        sa.CheckConstraint(
            "status IN ('received','queued','sent','failed','delivered','read')",
            name="chk_messages_status",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "seller_id"],
            ["conversations.id", "conversations.seller_id"],
            name="fk_messages_conversation_seller",
        ),
    )
    op.create_index("ix_messages_conversation_created", "messages", ["conversation_id", "created_at"])

    op.create_table(
        "orders",
        _id(),
        sa.Column("seller_id", UUID, nullable=False),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("customer_name", sa.Text(), nullable=False),
        sa.Column("customer_phone", sa.Text(), nullable=False),
        sa.Column("customer_address", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("total_pkr", sa.Integer(), nullable=False, server_default=sa.text("0")),
        _created(),
        _updated(),
        sa.UniqueConstraint("id", "seller_id", name="uq_orders_id_seller_id"),
        sa.CheckConstraint("status IN ('pending','confirmed','cancelled')", name="chk_orders_status"),
        sa.CheckConstraint("total_pkr >= 0", name="chk_orders_total_nonnegative"),
        sa.CheckConstraint(PHONE_CHECK, name="chk_orders_phone"),
        sa.ForeignKeyConstraint(
            ["conversation_id", "seller_id"],
            ["conversations.id", "conversations.seller_id"],
            name="fk_orders_conversation_seller",
        ),
    )
    op.create_index("ix_orders_seller_status", "orders", ["seller_id", "status"])

    op.create_table(
        "order_items",
        _id(),
        sa.Column("order_id", UUID, nullable=False),
        sa.Column("seller_id", UUID, nullable=False),
        sa.Column("product_id", UUID, nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("price_pkr_at_order", sa.Integer(), nullable=False),
        sa.Column("size", sa.Text(), nullable=True),
        sa.Column("color", sa.Text(), nullable=True),
        _created(),
        sa.CheckConstraint("qty > 0", name="chk_order_items_qty_positive"),
        sa.CheckConstraint("price_pkr_at_order >= 0", name="chk_order_items_price_nonnegative"),
        sa.ForeignKeyConstraint(
            ["order_id", "seller_id"],
            ["orders.id", "orders.seller_id"],
            name="fk_order_items_order_seller",
        ),
        sa.ForeignKeyConstraint(
            ["product_id", "seller_id"],
            ["products.id", "products.seller_id"],
            name="fk_order_items_product_seller",
            ondelete="RESTRICT",
        ),
    )
    op.execute(
        "ALTER TABLE order_items ADD CONSTRAINT uq_order_items_order_product_size_color "
        "UNIQUE NULLS NOT DISTINCT (order_id, product_id, size, color)"
    )
    op.create_index("ix_order_items_order_id", "order_items", ["order_id"])
    op.create_index("ix_order_items_product_id", "order_items", ["product_id"])

    op.create_table(
        "embeddings",
        _id(),
        sa.Column("seller_id", UUID, sa.ForeignKey("sellers.id"), nullable=False),
        sa.Column("product_id", UUID, nullable=True),
        sa.Column("faq_id", UUID, nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("vector", Vector(EMBEDDING_DIM), nullable=False),
        _created(),
        _updated(),
        sa.CheckConstraint(
            "(product_id IS NOT NULL AND faq_id IS NULL) OR (product_id IS NULL AND faq_id IS NOT NULL)",
            name="chk_embeddings_exactly_one_source",
        ),
        sa.ForeignKeyConstraint(
            ["product_id", "seller_id"],
            ["products.id", "products.seller_id"],
            name="fk_embeddings_product_seller",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["faq_id", "seller_id"],
            ["faqs.id", "faqs.seller_id"],
            name="fk_embeddings_faq_seller",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "uq_embeddings_seller_product", "embeddings", ["seller_id", "product_id"],
        unique=True, postgresql_where=sa.text("product_id IS NOT NULL"),
    )
    op.create_index(
        "uq_embeddings_seller_faq", "embeddings", ["seller_id", "faq_id"],
        unique=True, postgresql_where=sa.text("faq_id IS NOT NULL"),
    )
    op.create_index("ix_embeddings_seller_id", "embeddings", ["seller_id"])

    op.create_table(
        "audit_log",
        _id(),
        sa.Column("seller_id", UUID, sa.ForeignKey("sellers.id"), nullable=False),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", UUID, nullable=True),
        sa.Column("changes", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        _created(),
        sa.CheckConstraint(
            "action IN ('create','update','delete','login','logout','handoff_on','handoff_off','csv_upload','csv_reject')",
            name="chk_audit_action",
        ),
        sa.CheckConstraint(
            "entity_type IN ('product','faq','order','conversation','user')",
            name="chk_audit_entity_type",
        ),
    )
    op.create_index("ix_audit_log_seller_created", "audit_log", ["seller_id", "created_at"])


def downgrade() -> None:
    for table in (
        "audit_log", "embeddings", "order_items", "orders", "messages",
        "conversations", "faqs", "products", "users", "sellers",
    ):
        op.drop_table(table)