import hashlib
import math
import os
import random
import time
from abc import ABC, abstractmethod
from typing import Optional, Sequence
import uuid

import httpx
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import EMBEDDING_DIM, Embeddings, Faqs, Products

MAX_BATCH_SIZE = 100
# A maximum 2,000-row CSV and batches of 100 means at most 20 provider requests.
MAX_EMBED_CALLS_PER_UPLOAD = 20
# Placeholder pending calibration against a labeled seller catalog.
DEFAULT_SIMILARITY_THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", "0.65"))


def product_embed_content(
    name: str,
    description: Optional[str] = None,
    category: Optional[str] = None,
    sizes: Optional[Sequence[str]] = None,
    colors: Optional[Sequence[str]] = None,
) -> str:
    parts = [f"Product: {name.strip()}"]
    if description and description.strip():
        parts.append(f"Description: {description.strip()}")
    if category and category.strip():
        parts.append(f"Category: {category.strip()}")
    if sizes:
        parts.append(f"Sizes: {', '.join(sizes)}")
    if colors:
        parts.append(f"Colors: {', '.join(colors)}")
    return " | ".join(parts)


def faq_embed_content(question: str, answer: str) -> str:
    return f"Q: {question.strip()} | A: {answer.strip()}"


def compute_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class EmbeddingProvider(ABC):
    @property
    @abstractmethod
    def dimension(self) -> int:
        pass

    @abstractmethod
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        pass

    def embed_query(self, text: str) -> list[float]:
        results = self.embed_texts([text])
        return results[0]

    def validate_dimension(self) -> None:
        if self.dimension != EMBEDDING_DIM:
            raise ValueError(
                f"EmbeddingProvider dimension mismatch: expected exactly {EMBEDDING_DIM}, got {self.dimension}. "
                "Never truncate or pad embeddings silently."
            )


class FakeEmbeddingProvider(EmbeddingProvider):
    """
    Deterministic mock embedding provider for unit tests and local eval.
    Generates unit-normalized 1536-dimensional vectors based on content hash.
    """
    def __init__(self, target_dim: int = EMBEDDING_DIM):
        self._dimension = target_dim
        self.call_count = 0
        self.validate_dimension()

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.call_count += len(texts)
        embeddings = []
        for t in texts:
            # Deterministic pseudo-random vector derived from string hash
            h = hashlib.sha256(t.encode("utf-8")).digest()
            seed = int.from_bytes(h[:8], "big")
            rng = random.Random(seed)
            # Generate random gaussian components
            raw_vec = [rng.gauss(0.0, 1.0) for _ in range(self.dimension)]
            # Add semantic keyword bias for eval relevance
            keywords = t.lower()
            for token, idx_offset in [
                ("kurta", 10), ("kurti", 15), ("shalwar", 30), ("dupatta", 50),
                ("khussa", 70), ("sandal", 90), ("chappal", 90), ("bag", 110),
                ("shawl", 130), ("pashmina", 130), ("kids", 150), ("bachon", 150),
                ("waistcoat", 170), ("eid", 170), ("delivery", 200), ("return", 220),
            ]:
                if token in keywords:
                    for i in range(idx_offset, idx_offset + 10):
                        raw_vec[i % self.dimension] += 3.0

            # Normalize to unit length for accurate cosine similarity
            norm = math.sqrt(sum(x * x for x in raw_vec)) or 1.0
            vec = [x / norm for x in raw_vec]
            embeddings.append(vec)
        return embeddings


class OpenAIEmbeddings(EmbeddingProvider):
    """
    OpenAI embeddings provider (default model: text-embedding-3-small, dimension: 1536).
    """
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "text-embedding-3-small",
        dimensions: int = EMBEDDING_DIM,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
    ):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model
        self._dimension = dimensions
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.validate_dimension()

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY environment variable is not configured")

        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), MAX_BATCH_SIZE):
            batch = texts[i : i + MAX_BATCH_SIZE]
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "input": batch,
                "model": self.model,
                "dimensions": self._dimension,
            }

            last_exc = None
            for attempt in range(self.max_retries):
                try:
                    with httpx.Client(timeout=self.timeout_seconds) as client:
                        resp = client.post(
                            "https://api.openai.com/v1/embeddings",
                            headers=headers,
                            json=payload,
                        )
                    if resp.status_code == 200:
                        data = resp.json()
                        # Sort by index to maintain ordering
                        sorted_items = sorted(data["data"], key=lambda x: x["index"])
                        for item in sorted_items:
                            vec = item["embedding"]
                            if len(vec) != self.dimension:
                                raise ValueError(
                                    f"OpenAI returned vector of dimension {len(vec)}, expected {self.dimension}"
                                )
                            all_embeddings.append(vec)
                        break
                    else:
                        last_exc = RuntimeError(f"OpenAI API returned HTTP {resp.status_code}")
                except Exception as exc:
                    last_exc = RuntimeError(f"request failed ({type(exc).__name__})")

                if attempt < self.max_retries - 1:
                    time.sleep(0.5 * (2 ** attempt))
            else:
                raise RuntimeError(f"OpenAI embedding request failed after {self.max_retries} attempts: {last_exc}")

        return all_embeddings


class GeminiEmbeddings(EmbeddingProvider):
    """
    Google Gemini embeddings provider (model: text-embedding-004 or gemini-embedding-exp-03-07).
    Doc reference: https://ai.google.dev/gemini-api/docs/embeddings
    To request specific output dimensionality, use the `output_dimensionality` parameter.
    """
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        dimensions: int = EMBEDDING_DIM,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
    ):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-embedding-001")
        self._dimension = dimensions
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.validate_dimension()

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY / GOOGLE_API_KEY environment variable is not configured")

        all_embeddings: list[list[float]] = []
        model_name = self.model if self.model.startswith("models/") else f"models/{self.model}"
        url = f"https://generativelanguage.googleapis.com/v1beta/{model_name}:batchEmbedContents"
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

        for i in range(0, len(texts), MAX_BATCH_SIZE):
            batch = texts[i : i + MAX_BATCH_SIZE]
            requests = [
                {
                    "model": model_name,
                    "content": {"parts": [{"text": text_item}]},
                    "outputDimensionality": self._dimension,
                }
                for text_item in batch
            ]
            payload = {"requests": requests}

            last_exc = None
            for attempt in range(self.max_retries):
                try:
                    with httpx.Client(timeout=self.timeout_seconds) as client:
                        resp = client.post(url, headers=headers, json=payload)
                    if resp.status_code == 200:
                        data = resp.json()
                        embeddings_data = data.get("embeddings", [])
                        if len(embeddings_data) != len(batch):
                            raise ValueError(
                                f"Gemini returned {len(embeddings_data)} vectors for {len(batch)} inputs"
                            )
                        for item in embeddings_data:
                            vec = item.get("values", [])
                            if len(vec) != self.dimension:
                                raise ValueError(
                                    f"Gemini returned vector of dimension {len(vec)}, expected {self.dimension}"
                                )
                            all_embeddings.append(vec)
                        break
                    else:
                        # Do not include response bodies or request URLs (which contain the key).
                        last_exc = RuntimeError(f"Gemini API returned HTTP {resp.status_code}")
                except Exception as exc:
                    last_exc = RuntimeError(f"request failed ({type(exc).__name__})")

                if attempt < self.max_retries - 1:
                    time.sleep(0.5 * (2 ** attempt))
            else:
                raise RuntimeError(f"Gemini embedding request failed after {self.max_retries} attempts: {last_exc}")

        return all_embeddings


_default_provider: Optional[EmbeddingProvider] = None


def get_embedding_provider() -> EmbeddingProvider:
    global _default_provider
    if _default_provider is not None:
        return _default_provider

    provider_name = os.environ.get("EMBEDDING_PROVIDER", "openai").strip().lower()
    if provider_name == "openai":
        provider = OpenAIEmbeddings()
    elif provider_name == "gemini":
        provider = GeminiEmbeddings()
    elif provider_name in ("fake", "mock", "test"):
        provider = FakeEmbeddingProvider()
    else:
        raise ValueError(f"Unknown EMBEDDING_PROVIDER: {provider_name}. Expected 'openai' or 'gemini'.")

    provider.validate_dimension()
    if isinstance(provider, OpenAIEmbeddings) and not provider.api_key:
        raise RuntimeError("OPENAI_API_KEY must be configured when EMBEDDING_PROVIDER=openai")
    if isinstance(provider, GeminiEmbeddings) and not provider.api_key:
        raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) must be configured when EMBEDDING_PROVIDER=gemini")
    _default_provider = provider
    return provider


def set_embedding_provider(provider: Optional[EmbeddingProvider]) -> None:
    global _default_provider
    _default_provider = provider


def sync_product_embeddings(
    db: Session,
    seller_id: uuid.UUID,
    products: Sequence[Products],
    provider: Optional[EmbeddingProvider] = None,
) -> int:
    """
    Syncs product embeddings into Postgres.
    Checks sha256 content_hash and only calls the provider when hash changed.
    Returns number of newly embedded / updated items.
    """
    if not products:
        return 0

    if provider is None:
        provider = get_embedding_provider()

    prod_ids = [p.id for p in products]
    existing_hashes = dict(
        db.execute(
            select(Embeddings.product_id, Embeddings.content_hash).where(
                Embeddings.seller_id == seller_id,
                Embeddings.product_id.in_(prod_ids),
            )
        ).all()
    )

    to_embed: list[tuple[Products, str, str]] = []
    for p in products:
        content = product_embed_content(p.name, p.description, p.category, p.sizes, p.colors)
        c_hash = compute_content_hash(content)
        if existing_hashes.get(p.id) != c_hash:
            to_embed.append((p, content, c_hash))

    if not to_embed:
        return 0

    call_count = (len(to_embed) + MAX_BATCH_SIZE - 1) // MAX_BATCH_SIZE
    if call_count > MAX_EMBED_CALLS_PER_UPLOAD:
        raise RuntimeError(
            f"Embedding API call count ({call_count}) exceeds upload limit of {MAX_EMBED_CALLS_PER_UPLOAD}"
        )

    texts = [item[1] for item in to_embed]
    vectors = provider.embed_texts(texts)
    _validate_vectors(vectors, len(texts), provider)

    for (p, content, c_hash), vec in zip(to_embed, vectors):
        stmt = insert(Embeddings).values(
            seller_id=seller_id,
            product_id=p.id,
            faq_id=None,
            content=content,
            content_hash=c_hash,
            vector=vec,
        )
        excluded = stmt.excluded
        db.execute(
            stmt.on_conflict_do_update(
                index_elements=[Embeddings.seller_id, Embeddings.product_id],
                index_where=text("product_id IS NOT NULL"),
                set_={
                    "content": excluded.content,
                    "content_hash": excluded.content_hash,
                    "vector": excluded.vector,
                    "updated_at": text("now()"),
                },
            )
        )

    return len(to_embed)


def sync_faq_embeddings(
    db: Session,
    seller_id: uuid.UUID,
    faqs: Sequence[Faqs],
    provider: Optional[EmbeddingProvider] = None,
) -> int:
    """
    Syncs FAQ embeddings into Postgres.
    Checks sha256 content_hash and only calls the provider when hash changed.
    """
    if not faqs:
        return 0

    if provider is None:
        provider = get_embedding_provider()

    faq_ids = [f.id for f in faqs]
    existing_hashes = dict(
        db.execute(
            select(Embeddings.faq_id, Embeddings.content_hash).where(
                Embeddings.seller_id == seller_id,
                Embeddings.faq_id.in_(faq_ids),
            )
        ).all()
    )

    to_embed: list[tuple[Faqs, str, str]] = []
    for f in faqs:
        content = faq_embed_content(f.question, f.answer)
        c_hash = compute_content_hash(content)
        if existing_hashes.get(f.id) != c_hash:
            to_embed.append((f, content, c_hash))

    if not to_embed:
        return 0

    texts = [item[1] for item in to_embed]
    call_count = (len(texts) + MAX_BATCH_SIZE - 1) // MAX_BATCH_SIZE
    if call_count > MAX_EMBED_CALLS_PER_UPLOAD:
        raise RuntimeError(
            f"Embedding API call count ({call_count}) exceeds upload limit of {MAX_EMBED_CALLS_PER_UPLOAD}"
        )
    vectors = provider.embed_texts(texts)
    _validate_vectors(vectors, len(texts), provider)

    for (f, content, c_hash), vec in zip(to_embed, vectors):
        stmt = insert(Embeddings).values(
            seller_id=seller_id,
            product_id=None,
            faq_id=f.id,
            content=content,
            content_hash=c_hash,
            vector=vec,
        )
        excluded = stmt.excluded
        db.execute(
            stmt.on_conflict_do_update(
                index_elements=[Embeddings.seller_id, Embeddings.faq_id],
                index_where=text("faq_id IS NOT NULL"),
                set_={
                    "content": excluded.content,
                    "content_hash": excluded.content_hash,
                    "vector": excluded.vector,
                    "updated_at": text("now()"),
                },
            )
        )

    return len(to_embed)


def _validate_vectors(vectors: list[list[float]], expected_count: int, provider: EmbeddingProvider) -> None:
    if len(vectors) != expected_count:
        raise ValueError(
            f"{type(provider).__name__} returned {len(vectors)} vectors for {expected_count} inputs"
        )
    for vector in vectors:
        if len(vector) != EMBEDDING_DIM:
            raise ValueError(
                f"{type(provider).__name__} returned vector dimension {len(vector)}; expected exactly {EMBEDDING_DIM}"
            )
