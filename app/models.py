from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean, CheckConstraint, Column, DateTime, ForeignKey, ForeignKeyConstraint,
    Index, Integer, String, Text, UniqueConstraint, text,
)
from sqlalchemy.dialects import postgresql

from app.database import Base

EMBEDDING_DIM = 1536
UUID = postgresql.UUID(as_uuid=True)
PHONE_CHECK = "customer_phone ~ '^[1-9][0-9]{7,14}$'"


def _id():
    return Column(UUID, primary_key=True, server_default=text("gen_random_uuid()"))


def _created():
    return Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


def _updated():
    return Column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class Sellers(Base):
    __tablename__ = "sellers"
    id = _id()
    name = Column(Text, nullable=False)
    whatsapp_phone_number_id = Column(Text, nullable=True)
    whatsapp_access_token_encrypted = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = _created()
    updated_at = _updated()
    __table_args__ = (
        UniqueConstraint("whatsapp_phone_number_id", name="uq_sellers_whatsapp_phone_number_id"),
    )


class Users(Base):
    __tablename__ = "users"
    id = _id()
    seller_id = Column(UUID, ForeignKey("sellers.id"), nullable=False)
    email = Column(Text, nullable=False)
    password_hash = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, server_default=text("true"))
    last_login = Column(DateTime(timezone=True), nullable=True)
    created_at = _created()
    updated_at = _updated()
    __table_args__ = (
        UniqueConstraint("seller_id", name="uq_users_seller_id"),
        UniqueConstraint("id", "seller_id", name="uq_users_id_seller_id"),
        CheckConstraint("email = lower(email)", name="chk_users_email_lower"),
        Index("uq_users_lower_email", text("lower(email)"), unique=True),
    )


class Products(Base):
    __tablename__ = "products"
    id = _id()
    seller_id = Column(UUID, ForeignKey("sellers.id"), nullable=False)
    sku = Column(Text, nullable=False)
    name = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    price_pkr = Column(Integer, nullable=False)
    category = Column(Text, nullable=True)
    sizes = Column(postgresql.ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"))
    colors = Column(postgresql.ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"))
    stock = Column(Integer, nullable=False, server_default=text("0"))
    is_active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = _created()
    updated_at = _updated()
    __table_args__ = (
        UniqueConstraint("seller_id", "sku", name="uq_products_seller_sku"),
        UniqueConstraint("id", "seller_id", name="uq_products_id_seller_id"),
        CheckConstraint("price_pkr > 0", name="chk_products_price_positive"),
        CheckConstraint("stock >= 0", name="chk_products_stock_nonnegative"),
        CheckConstraint("cardinality(sizes) <= 20", name="chk_products_sizes_len"),
        CheckConstraint("cardinality(colors) <= 20", name="chk_products_colors_len"),
    )


class Faqs(Base):
    __tablename__ = "faqs"
    id = _id()
    seller_id = Column(UUID, ForeignKey("sellers.id"), nullable=False)
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, server_default=text("true"))
    created_at = _created()
    updated_at = _updated()
    __table_args__ = (
        UniqueConstraint("id", "seller_id", name="uq_faqs_id_seller_id"),
        Index("ix_faqs_seller_id", "seller_id"),
    )


class Conversations(Base):
    __tablename__ = "conversations"
    id = _id()
    seller_id = Column(UUID, ForeignKey("sellers.id"), nullable=False)
    customer_phone = Column(Text, nullable=False)
    customer_name = Column(Text, nullable=True)
    last_customer_message_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    bot_active = Column(Boolean, nullable=False, server_default=text("true"))
    needs_seller_action = Column(Boolean, nullable=False, server_default=text("false"))
    handoff_reason = Column(Text, nullable=True)
    created_at = _created()
    updated_at = _updated()
    __table_args__ = (
        UniqueConstraint("seller_id", "customer_phone", name="uq_conversations_seller_phone"),
        UniqueConstraint("id", "seller_id", name="uq_conversations_id_seller_id"),
        CheckConstraint(PHONE_CHECK, name="chk_conversations_phone"),
        Index("ix_conversations_seller_last_msg", "seller_id", "last_customer_message_at"),
    )


class Messages(Base):
    __tablename__ = "messages"
    id = _id()
    conversation_id = Column(UUID, nullable=False)
    seller_id = Column(UUID, nullable=False)
    whatsapp_message_id = Column(Text, nullable=True)
    direction = Column(Text, nullable=False)
    message_type = Column(Text, nullable=False)
    status = Column(Text, nullable=False)
    content = Column(Text, nullable=False)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    read_at = Column(DateTime(timezone=True), nullable=True)
    created_at = _created()
    updated_at = _updated()
    __table_args__ = (
        UniqueConstraint("whatsapp_message_id", name="uq_messages_whatsapp_message_id"),
        CheckConstraint("direction IN ('inbound','outbound')", name="chk_messages_direction"),
        CheckConstraint("message_type = 'text'", name="chk_messages_type"),
        CheckConstraint(
            "status IN ('received','queued','sent','failed','delivered','read')",
            name="chk_messages_status",
        ),
        ForeignKeyConstraint(
            ["conversation_id", "seller_id"],
            ["conversations.id", "conversations.seller_id"],
            name="fk_messages_conversation_seller",
        ),
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )


class Orders(Base):
    __tablename__ = "orders"
    id = _id()
    seller_id = Column(UUID, nullable=False)
    conversation_id = Column(UUID, nullable=False)
    customer_name = Column(Text, nullable=False)
    customer_phone = Column(Text, nullable=False)
    customer_address = Column(Text, nullable=True)
    status = Column(Text, nullable=False, server_default=text("'pending'"))
    total_pkr = Column(Integer, nullable=False, server_default=text("0"))
    created_at = _created()
    updated_at = _updated()
    __table_args__ = (
        UniqueConstraint("id", "seller_id", name="uq_orders_id_seller_id"),
        CheckConstraint("status IN ('pending','confirmed','cancelled')", name="chk_orders_status"),
        CheckConstraint("total_pkr >= 0", name="chk_orders_total_nonnegative"),
        CheckConstraint(PHONE_CHECK, name="chk_orders_phone"),
        ForeignKeyConstraint(
            ["conversation_id", "seller_id"],
            ["conversations.id", "conversations.seller_id"],
            name="fk_orders_conversation_seller",
        ),
        Index("ix_orders_seller_status", "seller_id", "status"),
    )


class OrderItems(Base):
    __tablename__ = "order_items"
    id = _id()
    order_id = Column(UUID, nullable=False)
    seller_id = Column(UUID, nullable=False)
    product_id = Column(UUID, nullable=False)
    qty = Column(Integer, nullable=False)
    price_pkr_at_order = Column(Integer, nullable=False)
    size = Column(Text, nullable=True)
    color = Column(Text, nullable=True)
    created_at = _created()
    __table_args__ = (
        CheckConstraint("qty > 0", name="chk_order_items_qty_positive"),
        CheckConstraint("price_pkr_at_order >= 0", name="chk_order_items_price_nonnegative"),
        UniqueConstraint(
            "order_id", "product_id", "size", "color",
            name="uq_order_items_order_product_size_color",
            postgresql_nulls_not_distinct=True,
        ),
        ForeignKeyConstraint(
            ["order_id", "seller_id"], ["orders.id", "orders.seller_id"],
            name="fk_order_items_order_seller",
        ),
        ForeignKeyConstraint(
            ["product_id", "seller_id"], ["products.id", "products.seller_id"],
            name="fk_order_items_product_seller", ondelete="RESTRICT",
        ),
        Index("ix_order_items_order_id", "order_id"),
        Index("ix_order_items_product_id", "product_id"),
    )


class Embeddings(Base):
    __tablename__ = "embeddings"
    id = _id()
    seller_id = Column(UUID, ForeignKey("sellers.id"), nullable=False)
    product_id = Column(UUID, nullable=True)
    faq_id = Column(UUID, nullable=True)
    content = Column(Text, nullable=False)
    content_hash = Column(String(64), nullable=False)
    vector = Column(Vector(EMBEDDING_DIM), nullable=False)
    created_at = _created()
    updated_at = _updated()
    __table_args__ = (
        CheckConstraint(
            "(product_id IS NOT NULL AND faq_id IS NULL) OR (product_id IS NULL AND faq_id IS NOT NULL)",
            name="chk_embeddings_exactly_one_source",
        ),
        ForeignKeyConstraint(
            ["product_id", "seller_id"], ["products.id", "products.seller_id"],
            name="fk_embeddings_product_seller", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["faq_id", "seller_id"], ["faqs.id", "faqs.seller_id"],
            name="fk_embeddings_faq_seller", ondelete="CASCADE",
        ),
        Index("uq_embeddings_seller_product", "seller_id", "product_id", unique=True,
              postgresql_where=text("product_id IS NOT NULL")),
        Index("uq_embeddings_seller_faq", "seller_id", "faq_id", unique=True,
              postgresql_where=text("faq_id IS NOT NULL")),
        Index("ix_embeddings_seller_id", "seller_id"),
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = _id()
    seller_id = Column(UUID, ForeignKey("sellers.id"), nullable=False)
    user_id = Column(UUID, ForeignKey("users.id"), nullable=True)
    action = Column(Text, nullable=False)
    entity_type = Column(Text, nullable=False)
    entity_id = Column(UUID, nullable=True)
    changes = Column(postgresql.JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    ip_address = Column(postgresql.INET, nullable=True)
    user_agent = Column(Text, nullable=True)
    created_at = _created()
    __table_args__ = (
        CheckConstraint(
            "action IN ('create','update','delete','login','logout','handoff_on','handoff_off','csv_upload','csv_reject')",
            name="chk_audit_action",
        ),
        CheckConstraint(
            "entity_type IN ('product','faq','order','conversation','user')",
            name="chk_audit_entity_type",
        ),
        Index("ix_audit_log_seller_created", "seller_id", "created_at"),
    )


class Sessions(Base):
    __tablename__ = "sessions"
    id = _id()
    token_hash = Column(Text, nullable=False)
    user_id = Column(UUID, nullable=False)
    seller_id = Column(UUID, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = _created()
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
        ForeignKeyConstraint(
            ["user_id", "seller_id"], ["users.id", "users.seller_id"],
            name="fk_sessions_user_seller", ondelete="CASCADE",
        ),
        Index("ix_sessions_user_id", "user_id"),
        Index("ix_sessions_expires_at", "expires_at"),
    )