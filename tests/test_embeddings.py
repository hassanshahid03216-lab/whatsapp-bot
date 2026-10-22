import pytest
from sqlalchemy import text

from app.embeddings import (
    FakeEmbeddingProvider,
    compute_content_hash,
    product_embed_content,
    sync_product_embeddings,
)
from app.models import EMBEDDING_DIM


def test_hash_unchanged_skips_provider(db_session):
    seller_id = db_session.execute(text("INSERT INTO sellers (name) VALUES ('hash-test') RETURNING id")).scalar_one()
    product_id = db_session.execute(text(
        "INSERT INTO products (seller_id, sku, name, price_pkr) VALUES (:s, 'HASH', 'Neeli kurta', 100) RETURNING id"
    ), {"s": seller_id}).scalar_one()
    from sqlalchemy import select
    from app.models import Products
    product = db_session.scalar(select(Products).where(Products.id == product_id))
    provider = FakeEmbeddingProvider()
    sync_product_embeddings(db_session, seller_id, [product], provider)
    assert provider.call_count == 1
    sync_product_embeddings(db_session, seller_id, [product], provider)
    assert provider.call_count == 1


def test_product_content_hash_handles_nulls():
    content = product_embed_content("  Kurta ", None, None, None, None)
    assert "Kurta" in content
    assert compute_content_hash(content) == compute_content_hash(content)


def test_dimension_mismatch_fails_immediately():
    with pytest.raises(ValueError, match="expected exactly 1536"):
        FakeEmbeddingProvider(target_dim=768)


def test_startup_rejects_dimension_mismatch(monkeypatch):
    from app import main

    class WrongDimension(FakeEmbeddingProvider):
        @property
        def dimension(self):
            return 768

    monkeypatch.setattr(main, "get_embedding_provider", WrongDimension)
    with pytest.raises(ValueError, match="expected exactly 1536"):
        main.validate_embedding_provider_at_startup()


def test_sync_rejects_missing_provider_vectors(db_session):
    seller_id = db_session.execute(text("INSERT INTO sellers (name) VALUES ('vector-test') RETURNING id")).scalar_one()
    from sqlalchemy import select
    from app.models import Products
    product_id = db_session.execute(text(
        "INSERT INTO products (seller_id, sku, name, price_pkr) VALUES (:s, 'VECTOR', 'Shirt', 100) RETURNING id"
    ), {"s": seller_id}).scalar_one()
    product = db_session.scalar(select(Products).where(Products.id == product_id))

    class ShortProvider(FakeEmbeddingProvider):
        def embed_texts(self, texts):
            return []

    with pytest.raises(ValueError, match="returned 0 vectors"):
        sync_product_embeddings(db_session, seller_id, [product], ShortProvider())


class AxisProvider(FakeEmbeddingProvider):
    def embed_texts(self, texts):
        self.call_count += len(texts)
        return [[1.0] + [0.0] * (EMBEDDING_DIM - 1) for _ in texts]


def _seed_retrieval_product(db_session, seller, sku, active=True, vector="[1," + ",".join(["0"] * 1535) + "]"):
    product_id = db_session.execute(text(
        "INSERT INTO products (seller_id, sku, name, price_pkr, is_active) "
        "VALUES (:s, :sku, :sku, 100, :active) RETURNING id"
    ), {"s": seller, "sku": sku, "active": active}).scalar_one()
    db_session.execute(text(
        "INSERT INTO embeddings (seller_id, product_id, content, content_hash, vector) "
        "VALUES (:s, :p, 'catalog text', :hash, CAST(:vector AS vector))"
    ), {"s": seller, "p": product_id, "hash": sku.ljust(64, "0"), "vector": vector})
    return product_id


def test_deactivated_product_is_not_retrievable(db_session):
    from app.retrieval import retrieve_relevant
    seller = db_session.execute(text("INSERT INTO sellers (name) VALUES ('inactive') RETURNING id")).scalar_one()
    _seed_retrieval_product(db_session, seller, "OFF", active=False)
    result = retrieve_relevant(db_session, seller, "query", provider=AxisProvider())
    assert result.matches == []


def test_retrieval_is_seller_scoped(db_session):
    from app.retrieval import retrieve_relevant
    seller_a = db_session.execute(text("INSERT INTO sellers (name) VALUES ('seller-a') RETURNING id")).scalar_one()
    seller_b = db_session.execute(text("INSERT INTO sellers (name) VALUES ('seller-b') RETURNING id")).scalar_one()
    _seed_retrieval_product(db_session, seller_a, "A")
    _seed_retrieval_product(db_session, seller_b, "B")
    result = retrieve_relevant(db_session, seller_a, "query", provider=AxisProvider())
    assert [match.product["sku"] for match in result.matches] == ["A"]
    assert "price_pkr" not in result.matches[0].product
    assert "stock" not in result.matches[0].product


def test_below_threshold_is_low_confidence(db_session):
    from app.retrieval import retrieve_relevant
    seller = db_session.execute(text("INSERT INTO sellers (name) VALUES ('low-score') RETURNING id")).scalar_one()
    opposite = "[-1," + ",".join(["0"] * 1535) + "]"
    _seed_retrieval_product(db_session, seller, "LOW", vector=opposite)
    result = retrieve_relevant(db_session, seller, "query", threshold=0.5, provider=AxisProvider())
    assert result.matches
    assert result.high_confidence is False
