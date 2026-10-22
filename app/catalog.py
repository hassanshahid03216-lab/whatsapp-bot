import csv
import io
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.database import get_db
from app.embeddings import sync_product_embeddings
from app.deps import Auth, _inet, client_ip, csrf_protect, get_auth, utcnow
from app.models import AuditLog, Products

router = APIRouter()
HEADERS = ["sku", "name", "description", "price_pkr", "sizes", "colors", "stock", "category"]
MAX_BYTES = 2 * 1024 * 1024


def _text(value: str, max_len: int, label: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()) or len(value) > max_len:
        raise ValueError(f"{label} must be {'empty or ' if allow_empty else ''}at most {max_len} characters")
    if value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        # Formula injection: any text cell starting with =, +, -, @, \t, or \r is rejected (not silently modified)
        # to prevent spreadsheet formula execution while ensuring seller data is never silently mutated.
        raise ValueError(f"{label} starts with a forbidden spreadsheet formula character")
    return value


def _items(value: str) -> list[str]:
    if value == "":
        return []
    parts = value.split("|")
    if len(parts) > 20 or any(not 1 <= len(p) <= 30 for p in parts) or len(set(parts)) != len(parts):
        raise ValueError("must contain up to 20 unique non-empty items of 1 to 30 characters")
    for p in parts:
        _text(p, 30, "item")
    return parts


class ProductFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(max_length=2000)
    price_pkr: int = Field(gt=0, strict=True)
    stock: int = Field(ge=0, strict=True)
    category: str = Field(max_length=100)
    sizes: list[str]
    colors: list[str]

    @field_validator("name", "description", "category")
    @classmethod
    def safe_text(cls, v: str, info) -> str:
        max_lens = {"name": 200, "description": 2000, "category": 100}
        field_name = info.field_name
        return _text(v, max_lens[field_name], field_name, allow_empty=(field_name != "name"))

    @field_validator("sizes", "colors")
    @classmethod
    def valid_items(cls, v: list[str]) -> list[str]:
        if len(v) > 20 or any(not 1 <= len(x) <= 30 for x in v) or len(set(v)) != len(v):
            raise ValueError("must contain up to 20 unique non-empty items of 1 to 30 characters")
        for item in v:
            _text(item, 30, "item")
        return v


class ProductPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    price_pkr: Optional[int] = Field(default=None, gt=0, strict=True)
    stock: Optional[int] = Field(default=None, ge=0, strict=True)
    category: Optional[str] = Field(default=None, max_length=100)
    sizes: Optional[list[str]] = None
    colors: Optional[list[str]] = None

    @field_validator("name", "description", "category")
    @classmethod
    def safe_text(cls, v: Optional[str], info) -> Optional[str]:
        if v is None:
            return v
        max_lens = {"name": 200, "description": 2000, "category": 100}
        field_name = info.field_name
        return _text(v, max_lens[field_name], field_name, allow_empty=(field_name != "name"))

    @field_validator("sizes", "colors")
    @classmethod
    def valid_items(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is not None:
            if len(v) > 20 or any(not 1 <= len(x) <= 30 for x in v) or len(set(v)) != len(v):
                raise ValueError("must contain up to 20 unique non-empty items of 1 to 30 characters")
            for x in v:
                _text(x, 30, "item")
        return v


def audit_product(request: Request, auth: Auth, action: str, entity_id: Optional[uuid.UUID], changes: dict) -> AuditLog:
    """changes holds counts and ids only; never cell contents, product names, or descriptions."""
    return AuditLog(
        seller_id=auth.seller_id,
        user_id=auth.user_id,
        action=action,
        entity_type="product",
        entity_id=entity_id,
        changes=changes,
        ip_address=_inet(client_ip(request)),
        user_agent=(request.headers.get("user-agent") or "")[:200] or None,
    )


def _dump(p: Products) -> dict:
    return {
        "id": str(p.id),
        "sku": p.sku,
        "name": p.name,
        "description": p.description,
        "price_pkr": p.price_pkr,
        "sizes": p.sizes,
        "colors": p.colors,
        "stock": p.stock,
        "category": p.category,
        "active": p.is_active,
    }


@router.post("/catalog/upload")
async def upload_catalog(
    request: Request,
    file: UploadFile | None = File(None),
    auth: Auth = Depends(csrf_protect),
    db: Session = Depends(get_db),
):
    def reject(status: int, detail: str) -> None:
        db.add(audit_product(request, auth, "csv_reject", None, {"errors": 1}))
        db.commit()
        raise HTTPException(status_code=status, detail=detail)

    if file is None:
        raise HTTPException(status_code=422, detail="A CSV file is required")
    if not file.filename or not file.filename.lower().endswith(".csv") or not file.content_type or not file.content_type.lower().startswith("text/"):
        reject(415, "CSV extension and text content type are required")

    raw = bytearray()
    while True:
        chunk = await file.read(65536)
        if not chunk:
            break
        raw.extend(chunk)
        if len(raw) > MAX_BYTES:
            reject(413, "CSV exceeds 2 MB")

    if b"\x00" in raw:
        reject(415, "NUL bytes are not accepted")

    try:
        decoded = bytes(raw).decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        reject(415, "File must be valid UTF-8")

    try:
        reader = csv.reader(io.StringIO(decoded, newline=""), strict=True)
        rows, line_numbers = [], []
        while True:
            try:
                rows.append(next(reader))
                line_numbers.append(reader.line_num)
            except StopIteration:
                break
    except csv.Error:
        reject(422, "Malformed CSV")

    if not rows or rows[0] != HEADERS:
        reject(422, "Header must exactly match sku,name,description,price_pkr,sizes,colors,stock,category")

    data = rows[1:]
    if len(data) > 2000:
        reject(422, "CSV has more than 2000 data rows")

    issues = []
    prepared = []
    seen = set()

    for row_index, row in enumerate(data, 1):
        line = line_numbers[row_index]
        if len(row) != len(HEADERS):
            issues.append({"row": line, "column": "row", "reason": "Expected exactly 8 columns"})
            continue

        values = dict(zip(HEADERS, row))
        row_errors = []

        try:
            sku = _text(values["sku"], 64, "sku")
            if any(c.isspace() for c in sku):
                raise ValueError("sku must not contain whitespace")
            if sku in seen:
                raise ValueError("duplicate sku in file")
        except ValueError as exc:
            row_errors.append(("sku", str(exc)))
            sku = None

        if sku is not None:
            seen.add(sku)

        field_values = {}
        for column in ("name", "description", "price_pkr", "stock", "category", "sizes", "colors"):
            try:
                if column in ("price_pkr", "stock"):
                    raw_val = values[column]
                    if not raw_val.isascii() or not raw_val.isdigit():
                        raise ValueError("must be a whole integer without signs, decimals, or separators")
                    field_values[column] = int(raw_val)
                elif column in ("sizes", "colors"):
                    field_values[column] = _items(values[column])
                else:
                    field_values[column] = values[column]
                if column in ("name", "description", "category"):
                    _text(field_values[column], {"name": 200, "description": 2000, "category": 100}[column], column, allow_empty=(column != "name"))
            except ValueError as exc:
                row_errors.append((column, str(exc)))

        if not row_errors:
            try:
                fields = ProductFields(**field_values)
            except ValidationError as exc:
                for error in exc.errors():
                    row_errors.append((str(error["loc"][0]), str(error["msg"])))

        if row_errors:
            issues.extend({"row": line, "column": col, "reason": rsn[:200]} for col, rsn in row_errors)
        else:
            prepared.append({"seller_id": auth.seller_id, "sku": sku, **fields.model_dump()})

    if issues:
        db.add(audit_product(request, auth, "csv_reject", None, {"rows": len(data), "errors": len(issues)}))
        db.commit()
        raise HTTPException(status_code=422, detail={"errors": issues[:100], "total": len(issues)})

    if prepared:
        # Reactivation choice: A re-uploaded SKU that was previously deactivated remains deactivated
        # (is_active is not updated on conflict) so bulk CSV uploads do not unintentionally reactivate items taken off sale.
        stmt = insert(Products).values(prepared)
        excluded = stmt.excluded
        existing = set(db.scalars(select(Products.sku).where(
            Products.seller_id == auth.seller_id,
            Products.sku.in_([item["sku"] for item in prepared]),
        )).all())
        db.execute(stmt.on_conflict_do_update(
            index_elements=[Products.seller_id, Products.sku],
            set_={
                "name": excluded.name,
                "description": excluded.description,
                "price_pkr": excluded.price_pkr,
                "sizes": excluded.sizes,
                "colors": excluded.colors,
                "stock": excluded.stock,
                "category": excluded.category,
                "updated_at": text("now()"),
            },
        ))
        updated = len(existing)
        created = len(prepared) - updated

        # Synchronize embeddings in the same transaction
        saved_products = db.scalars(select(Products).where(
            Products.seller_id == auth.seller_id,
            Products.sku.in_([item["sku"] for item in prepared]),
        )).all()
        sync_product_embeddings(db, auth.seller_id, saved_products)
    else:
        created = updated = 0

    db.add(audit_product(request, auth, "csv_upload", None, {"created": created, "updated": updated, "total": len(prepared)}))
    db.commit()
    return {"created": created, "updated": updated, "total": len(prepared)}


@router.get("/catalog/products")
def list_products(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    active: Optional[bool] = None,
    auth: Auth = Depends(get_auth),
    db: Session = Depends(get_db),
):
    q = select(Products).where(Products.seller_id == auth.seller_id)
    if active is not None:
        q = q.where(Products.is_active == active)
    items = db.scalars(q.order_by(Products.sku).limit(limit).offset(offset)).all()
    return {"items": [_dump(p) for p in items], "limit": limit, "offset": offset}


@router.get("/catalog/products/{product_id}")
def get_product(
    product_id: uuid.UUID,
    auth: Auth = Depends(get_auth),
    db: Session = Depends(get_db),
):
    p = db.scalar(select(Products).where(Products.id == product_id, Products.seller_id == auth.seller_id))
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    return _dump(p)


@router.patch("/catalog/products/{product_id}")
def patch_product(
    product_id: uuid.UUID,
    payload: ProductPatch,
    request: Request,
    auth: Auth = Depends(csrf_protect),
    db: Session = Depends(get_db),
):
    p = db.scalar(select(Products).where(Products.id == product_id, Products.seller_id == auth.seller_id))
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="At least one field is required")
    for k, v in changes.items():
        setattr(p, k, v)
    p.updated_at = utcnow()
    sync_product_embeddings(db, auth.seller_id, [p])
    db.add(audit_product(request, auth, "update", p.id, {"id": str(p.id)}))
    db.commit()
    return _dump(p)


def _set_product_active(
    product_id: uuid.UUID,
    active: bool,
    request: Request,
    auth: Auth,
    db: Session,
) -> dict:
    p = db.scalar(select(Products).where(Products.id == product_id, Products.seller_id == auth.seller_id))
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    p.is_active = active
    p.updated_at = utcnow()
    db.add(audit_product(request, auth, "update", p.id, {"id": str(p.id)}))
    db.commit()
    return _dump(p)


@router.post("/catalog/products/{product_id}/deactivate")
def deactivate_product(
    product_id: uuid.UUID,
    request: Request,
    auth: Auth = Depends(csrf_protect),
    db: Session = Depends(get_db),
):
    return _set_product_active(product_id, False, request, auth, db)


@router.post("/catalog/products/{product_id}/reactivate")
def reactivate_product(
    product_id: uuid.UUID,
    request: Request,
    auth: Auth = Depends(csrf_protect),
    db: Session = Depends(get_db),
):
    return _set_product_active(product_id, True, request, auth, db)
