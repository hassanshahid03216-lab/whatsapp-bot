import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.embeddings import sync_faq_embeddings
from app.deps import Auth, _inet, client_ip, csrf_protect, get_auth, utcnow
from app.models import AuditLog, Faqs

router = APIRouter()


class FaqIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1, max_length=2000)

    @field_validator("question", "answer")
    @classmethod
    def trimmed_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class FaqPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: Optional[str] = Field(default=None, min_length=1, max_length=500)
    answer: Optional[str] = Field(default=None, min_length=1, max_length=2000)

    @field_validator("question", "answer")
    @classmethod
    def trimmed_optional(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


def _dump(f: Faqs) -> dict:
    return {
        "id": str(f.id),
        "question": f.question,
        "answer": f.answer,
        "active": f.is_active,
    }


def audit_faq(request: Request, auth: Auth, action: str, entity_id: Optional[uuid.UUID], changes: dict) -> AuditLog:
    """changes holds ids only; never question or answer text."""
    return AuditLog(
        seller_id=auth.seller_id,
        user_id=auth.user_id,
        action=action,
        entity_type="faq",
        entity_id=entity_id,
        changes=changes,
        ip_address=_inet(client_ip(request)),
        user_agent=(request.headers.get("user-agent") or "")[:200] or None,
    )


@router.post("/faqs", status_code=201)
def create_faq(
    payload: FaqIn,
    request: Request,
    auth: Auth = Depends(csrf_protect),
    db: Session = Depends(get_db),
):
    f = Faqs(seller_id=auth.seller_id, question=payload.question, answer=payload.answer)
    db.add(f)
    db.flush()
    sync_faq_embeddings(db, auth.seller_id, [f])
    db.add(audit_faq(request, auth, "create", f.id, {"id": str(f.id)}))
    db.commit()
    return _dump(f)


@router.get("/faqs")
def list_faqs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    auth: Auth = Depends(get_auth),
    db: Session = Depends(get_db),
):
    q = select(Faqs).where(Faqs.seller_id == auth.seller_id)
    rows = db.scalars(q.order_by(Faqs.created_at, Faqs.id).limit(limit).offset(offset)).all()
    return {"items": [_dump(f) for f in rows], "limit": limit, "offset": offset}


@router.patch("/faqs/{faq_id}")
def patch_faq(
    faq_id: uuid.UUID,
    payload: FaqPatch,
    request: Request,
    auth: Auth = Depends(csrf_protect),
    db: Session = Depends(get_db),
):
    f = db.scalar(select(Faqs).where(Faqs.id == faq_id, Faqs.seller_id == auth.seller_id))
    if not f:
        raise HTTPException(status_code=404, detail="FAQ not found")
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="At least one field is required")
    for key, value in changes.items():
        setattr(f, key, value)
    f.updated_at = utcnow()
    sync_faq_embeddings(db, auth.seller_id, [f])
    db.add(audit_faq(request, auth, "update", f.id, {"id": str(f.id)}))
    db.commit()
    return _dump(f)


def _set_faq_active(
    faq_id: uuid.UUID,
    active: bool,
    request: Request,
    auth: Auth,
    db: Session,
) -> dict:
    f = db.scalar(select(Faqs).where(Faqs.id == faq_id, Faqs.seller_id == auth.seller_id))
    if not f:
        raise HTTPException(status_code=404, detail="FAQ not found")
    f.is_active = active
    f.updated_at = utcnow()
    db.add(audit_faq(request, auth, "update", f.id, {"id": str(f.id)}))
    db.commit()
    return _dump(f)


@router.post("/faqs/{faq_id}/deactivate")
def deactivate_faq(
    faq_id: uuid.UUID,
    request: Request,
    auth: Auth = Depends(csrf_protect),
    db: Session = Depends(get_db),
):
    return _set_faq_active(faq_id, False, request, auth, db)


@router.post("/faqs/{faq_id}/reactivate")
def reactivate_faq(
    faq_id: uuid.UUID,
    request: Request,
    auth: Auth = Depends(csrf_protect),
    db: Session = Depends(get_db),
):
    return _set_faq_active(faq_id, True, request, auth, db)
