from dataclasses import dataclass
from typing import Optional
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.embeddings import DEFAULT_SIMILARITY_THRESHOLD, EmbeddingProvider, get_embedding_provider
from app.models import EMBEDDING_DIM, Embeddings, Faqs, Products


@dataclass(frozen=True)
class RetrievalMatch:
    entity_type: str  # 'product' or 'faq'
    entity_id: uuid.UUID
    score: float
    product: Optional[dict] = None
    faq: Optional[dict] = None


@dataclass(frozen=True)
class RetrievalResult:
    matches: list[RetrievalMatch]
    high_confidence: bool
    top_score: float


def retrieve_relevant(
    db: Session,
    seller_id: uuid.UUID,
    query: str,
    top_k: int = 5,
    threshold: Optional[float] = None,
    provider: Optional[EmbeddingProvider] = None,
) -> RetrievalResult:
    """
    Embeds the user query, searches Postgres vector embeddings using cosine similarity,
    restricted strictly to the seller_id, joining only active products and active FAQs.
    Returns typed matches and a confidence flag.
    """
    if threshold is None:
        threshold = DEFAULT_SIMILARITY_THRESHOLD

    if provider is None:
        provider = get_embedding_provider()

    provider.validate_dimension()
    query_vec = provider.embed_query(query)
    if len(query_vec) != EMBEDDING_DIM:
        raise ValueError(
            f"Query embedding dimension mismatch: expected exactly {EMBEDDING_DIM}, got {len(query_vec)}"
        )
    top_k = min(max(top_k, 1), 5)

    # 1 - (vector <=> query_vec) gives cosine similarity in [-1, 1]
    # Filter joins to ensure deactivated products/FAQs are never returned
    stmt = (
        select(
            Embeddings.product_id,
            Embeddings.faq_id,
            (1 - Embeddings.vector.cosine_distance(query_vec)).label("similarity"),
            Products,
            Faqs,
        )
        .outerjoin(Products, (Embeddings.product_id == Products.id) & (Products.is_active.is_(True)))
        .outerjoin(Faqs, (Embeddings.faq_id == Faqs.id) & (Faqs.is_active.is_(True)))
        .where(
            Embeddings.seller_id == seller_id,
            (
                (Embeddings.product_id.is_not(None) & Products.id.is_not(None))
                | (Embeddings.faq_id.is_not(None) & Faqs.id.is_not(None))
            ),
        )
        .order_by(Embeddings.vector.cosine_distance(query_vec).asc())
        .limit(top_k)
    )

    rows = db.execute(stmt).all()
    matches: list[RetrievalMatch] = []

    for prod_id, faq_id, score, prod, faq in rows:
        sim_score = float(score)
        if prod_id is not None and prod is not None:
            p_dict = {
                "id": str(prod.id),
                "sku": prod.sku,
                "name": prod.name,
                "description": prod.description,
                "sizes": prod.sizes,
                "colors": prod.colors,
                "category": prod.category,
                "active": prod.is_active,
            }
            matches.append(
                RetrievalMatch(
                    entity_type="product",
                    entity_id=prod.id,
                    score=sim_score,
                    product=p_dict,
                )
            )
        elif faq_id is not None and faq is not None:
            f_dict = {
                "id": str(faq.id),
                "question": faq.question,
                "answer": faq.answer,
                "active": faq.is_active,
            }
            matches.append(
                RetrievalMatch(
                    entity_type="faq",
                    entity_id=faq.id,
                    score=sim_score,
                    faq=f_dict,
                )
            )

    top_score = matches[0].score if matches else 0.0
    high_confidence = bool(matches and top_score >= threshold)

    return RetrievalResult(
        matches=matches,
        high_confidence=high_confidence,
        top_score=top_score,
    )


def lookup_by_sku(db: Session, seller_id: uuid.UUID, sku: str) -> Optional[Products]:
    """
    Exact structured database lookup by SKU.
    Price and stock must NEVER come from embeddings or the LLM.
    """
    return db.scalar(
        select(Products).where(
            Products.seller_id == seller_id,
            Products.sku == sku.strip(),
            Products.is_active.is_(True),
        )
    )


def lookup_by_exact_name(db: Session, seller_id: uuid.UUID, name: str) -> Optional[Products]:
    """
    Exact structured database lookup by product name (case-insensitive).
    Price and stock must NEVER come from embeddings or the LLM.
    """
    return db.scalar(
        select(Products).where(
            Products.seller_id == seller_id,
            func.lower(Products.name) == name.strip().lower(),
            Products.is_active.is_(True),
        )
    )
