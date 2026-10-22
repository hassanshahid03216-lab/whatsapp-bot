"""Manual real-provider retrieval evaluation: python scripts/eval_retrieval.py --seller-id UUID"""
import argparse
from pathlib import Path
import uuid

import yaml
from sqlalchemy import select

from app.database import SessionLocal
from app.embeddings import get_embedding_provider
from app.models import Products
from app.retrieval import retrieve_relevant


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seller-id", required=True, help="Seller whose sample catalog was uploaded")
    args = parser.parse_args()
    seller_id = uuid.UUID(args.seller_id)
    cases = yaml.safe_load((Path(__file__).parents[1] / "tests/eval/questions.yaml").read_text(encoding="utf-8"))["questions"]
    provider = get_embedding_provider()
    provider.validate_dimension()
    results = []
    with SessionLocal() as db:
        for case in cases:
            result = retrieve_relevant(db, seller_id, case["query"], provider=provider)
            skus = [m.product["sku"] for m in result.matches if m.product]
            results.append((case["expected_sku"], skus, result.top_score))

        catalog_skus = set(db.scalars(select(Products.sku).where(Products.seller_id == seller_id)).all())
    if not catalog_skus:
        raise SystemExit("No products found for seller; upload samples/catalog.csv first")
    top1 = sum(bool(skus and skus[0] == expected) for expected, skus, _ in results) / len(results)
    top5 = sum(expected in skus for expected, skus, _ in results) / len(results)
    candidates = sorted({0.0, *(score for _, _, score in results), 1.0})
    best_threshold = max(candidates, key=lambda threshold: sum(
        score >= threshold and bool(skus and skus[0] == expected)
        for expected, skus, score in results
    ) / len(results))
    print(f"Questions: {len(results)}; catalog SKUs: {len(catalog_skus)}")
    print(f"Top-1 accuracy: {top1:.1%}")
    print(f"Top-5 accuracy: {top5:.1%}")
    print(f"Suggested SIMILARITY_THRESHOLD (best top-1 accuracy on this set): {best_threshold:.4f}")


if __name__ == "__main__":
    main()
