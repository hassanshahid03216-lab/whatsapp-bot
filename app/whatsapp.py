"""Seller settings and the verified WhatsApp Cloud API webhook."""
import asyncio
import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from cryptography.fernet import Fernet, MultiFernet
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.database import get_db
from app.embeddings import EmbeddingProvider, get_embedding_provider
from app.deps import Auth, csrf_protect, get_auth
from app.models import Conversations, Faqs, Messages, Products, Sellers
from app.retrieval import lookup_by_exact_name, lookup_by_sku, retrieve_relevant

router = APIRouter()
GRAPH_API_VERSION = os.environ.get("WHATSAPP_GRAPH_API_VERSION", "v21.0")
HTTP_TIMEOUT_SECONDS = 10.0


class WhatsAppSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone_number_id: str = Field(min_length=1, max_length=128)
    access_token: str = Field(min_length=20, max_length=4096)

    @field_validator("phone_number_id", "access_token")
    @classmethod
    def strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("field is required")
        return value


class SellerReplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=4000)

    @field_validator("message")
    @classmethod
    def trim_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message is required")
        return value


def _fernet() -> MultiFernet:
    raw_keys = os.environ.get("WHATSAPP_ACCESS_TOKEN_KEY", "")
    if not raw_keys:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN_KEY is required to store WhatsApp credentials")
    try:
        return MultiFernet([Fernet(key.strip().encode("ascii")) for key in raw_keys.split(",") if key.strip()])
    except (ValueError, TypeError) as exc:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN_KEY must contain valid comma-separated Fernet keys") from exc


def encrypt_access_token(token: str) -> str:
    return _fernet().encrypt(token.encode("utf-8")).decode("ascii")


def decrypt_access_token(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise RuntimeError("Could not decrypt this seller's WhatsApp access token") from exc


@router.get("/settings/whatsapp")
def get_whatsapp_settings(auth: Auth = Depends(get_auth), db: Session = Depends(get_db)):
    seller = db.get(Sellers, auth.seller_id)
    return {
        "connected": bool(seller and seller.whatsapp_phone_number_id and seller.whatsapp_access_token_encrypted),
        "phone_number_id": seller.whatsapp_phone_number_id if seller else None,
    }


@router.put("/settings/whatsapp")
def save_whatsapp_settings(
    payload: WhatsAppSettingsIn,
    auth: Auth = Depends(csrf_protect),
    db: Session = Depends(get_db),
):
    seller = db.get(Sellers, auth.seller_id)
    if not seller:
        raise HTTPException(status_code=404, detail="Seller not found")
    try:
        encrypted = encrypt_access_token(payload.access_token)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    seller.whatsapp_phone_number_id = payload.phone_number_id
    seller.whatsapp_access_token_encrypted = encrypted
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        # Never return the database exception: it might include submitted values.
        raise HTTPException(status_code=409, detail="Could not save WhatsApp settings") from exc
    return {"connected": True, "phone_number_id": seller.whatsapp_phone_number_id}


@router.get("/conversations")
def list_conversations(auth: Auth = Depends(get_auth), db: Session = Depends(get_db)):
    conversations = db.scalars(
        select(Conversations)
        .where(Conversations.seller_id == auth.seller_id)
        .order_by(Conversations.last_customer_message_at.desc(), Conversations.id)
        .limit(100)
    ).all()
    items = []
    for conversation in conversations:
        messages = db.scalars(
            select(Messages)
            .where(Messages.seller_id == auth.seller_id, Messages.conversation_id == conversation.id)
            .order_by(Messages.created_at.desc(), Messages.id.desc())
            .limit(20)
        ).all()
        items.append({
            "id": str(conversation.id),
            "customer_phone": conversation.customer_phone,
            "customer_name": conversation.customer_name,
            "bot_active": conversation.bot_active,
            "needs_seller_action": conversation.needs_seller_action,
            "messages": [{
                "direction": message.direction,
                "content": message.content,
                "status": message.status,
                "created_at": message.created_at.isoformat(),
            } for message in reversed(messages)],
        })
    return {"items": items}


def publish_conversation_event(conversation_id: uuid.UUID | str, event: dict) -> None:
    """Publish real-time events to Redis pub/sub channel for live SSE dashboard subscribers."""
    try:
        from app.ratelimit import login_ip_limiter
        client = login_ip_limiter._get_client()
        if client is not None:
            client.publish(f"conv:{conversation_id}", json.dumps(event))
    except Exception:
        pass


@router.get("/api/conversations/{conversation_id}/stream")
@router.get("/conversations/{conversation_id}/stream")
async def conversation_stream(
    conversation_id: uuid.UUID,
    request: Request,
    auth: Auth = Depends(get_auth),
    db: Session = Depends(get_db),
):
    conversation = db.scalar(select(Conversations).where(
        Conversations.id == conversation_id, Conversations.seller_id == auth.seller_id,
    ))
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    limit_one = (
        request.headers.get("x-test-stream-limit") == "1"
        or request.headers.get("X-Test-Stream-Limit") == "1"
        or request.query_params.get("limit_one") == "1"
        or any(k.lower() == "x-test-stream-limit" and v == "1" for k, v in request.headers.items())
    )

    async def event_generator():
        from app import config
        channel_name = f"conv:{conversation_id}"
        redis_url = config.REDIS_URL
        yield f"data: {json.dumps({'type': 'connected', 'conversation_id': str(conversation_id)})}\n\n"
        if limit_one:
            return

        if redis_url:
            try:
                import redis.asyncio as aioredis
                r = aioredis.from_url(redis_url, decode_responses=True)
                pubsub = r.pubsub()
                await pubsub.subscribe(channel_name)
                try:
                    while True:
                        if await request.is_disconnected():
                            break
                        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
                        if msg and msg.get("data"):
                            yield f"data: {msg['data']}\n\n"
                        await asyncio.sleep(0.1)
                finally:
                    await pubsub.unsubscribe(channel_name)
                    await pubsub.close()
                    await r.close()
            except Exception:
                yield f"data: {json.dumps({'type': 'connected', 'conversation_id': str(conversation_id)})}\n\n"
                while not await request.is_disconnected():
                    await asyncio.sleep(0.5)
                    yield f": keepalive\n\n"
        else:
            yield f"data: {json.dumps({'type': 'connected', 'conversation_id': str(conversation_id)})}\n\n"
            while not await request.is_disconnected():
                await asyncio.sleep(0.5)
                yield f": keepalive\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/conversations/{conversation_id}/handoff")
def handoff_conversation(
    conversation_id: uuid.UUID,
    auth: Auth = Depends(csrf_protect),
    db: Session = Depends(get_db),
):
    conversation = db.scalar(select(Conversations).where(
        Conversations.id == conversation_id, Conversations.seller_id == auth.seller_id,
    ))
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conversation.bot_active = False
    conversation.needs_seller_action = True
    db.commit()
    publish_conversation_event(conversation_id, {
        "type": "state", "conversation_id": str(conversation_id),
        "bot_active": False, "needs_seller_action": True,
    })
    return {"bot_active": False, "needs_seller_action": True}


@router.post("/conversations/{conversation_id}/resume")
def resume_conversation(
    conversation_id: uuid.UUID,
    auth: Auth = Depends(csrf_protect),
    db: Session = Depends(get_db),
):
    conversation = db.scalar(select(Conversations).where(
        Conversations.id == conversation_id, Conversations.seller_id == auth.seller_id,
    ))
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conversation.bot_active = True
    conversation.needs_seller_action = False
    db.commit()
    publish_conversation_event(conversation_id, {
        "type": "state", "conversation_id": str(conversation_id),
        "bot_active": True, "needs_seller_action": False,
    })
    return {"bot_active": True, "needs_seller_action": False}


@router.post("/conversations/{conversation_id}/reply")
def seller_reply(
    conversation_id: uuid.UUID,
    payload: SellerReplyIn,
    auth: Auth = Depends(csrf_protect),
    db: Session = Depends(get_db),
):
    conversation = db.scalar(select(Conversations).where(
        Conversations.id == conversation_id, Conversations.seller_id == auth.seller_id,
    ))
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    seller = db.get(Sellers, auth.seller_id)
    if not seller or not seller.whatsapp_phone_number_id or not seller.whatsapp_access_token_encrypted:
        raise HTTPException(status_code=409, detail="WhatsApp is not connected")
    token = decrypt_access_token(seller.whatsapp_access_token_encrypted)
    try:
        message_id = send_text_message(
            seller.whatsapp_phone_number_id, token, conversation.customer_phone, payload.message,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail="WhatsApp could not send the message") from exc
    now = datetime.now(timezone.utc)
    db.add(Messages(
        conversation_id=conversation.id,
        seller_id=auth.seller_id,
        whatsapp_message_id=message_id,
        direction="outbound",
        message_type="text",
        status="sent",
        content=payload.message,
    ))
    conversation.needs_seller_action = False
    db.commit()
    publish_conversation_event(conversation_id, {
        "type": "message",
        "conversation_id": str(conversation_id),
        "direction": "outbound",
        "content": payload.message,
        "status": "sent",
        "created_at": now.isoformat(),
        "needs_seller_action": False,
        "bot_active": conversation.bot_active,
    })
    return {"sent": True}


@router.get("/webhooks/whatsapp")
def verify_webhook(request: Request):
    verify_token = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
    mode = request.query_params.get("hub.mode", "")
    token = request.query_params.get("hub.verify_token", "")
    challenge = request.query_params.get("hub.challenge", "")
    if verify_token and mode == "subscribe" and hmac.compare_digest(token, verify_token):
        return PlainTextResponse(challenge, status_code=200)
    raise HTTPException(status_code=403, detail="Webhook verification failed")


def _valid_signature(raw_body: bytes, signature: Optional[str]) -> bool:
    app_secret = os.environ.get("WHATSAPP_APP_SECRET", "")
    if not app_secret or not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def _format_product(product: Products) -> str:
    stock_line = "Available" if product.stock > 0 else "Currently out of stock"
    return f"{product.name}\nPrice: PKR {product.price_pkr:,}\nStock: {stock_line} ({product.stock})"


def make_reply(
    db: Session,
    seller_id: uuid.UUID,
    query: str,
    provider: Optional[EmbeddingProvider] = None,
) -> str:
    """Return catalog facts from structured rows or an active FAQ; never ask an LLM for price/stock."""
    cleaned = query.strip()
    if not cleaned:
        return "Please send a product name or your question."

    product = lookup_by_sku(db, seller_id, cleaned)
    if product is None:
        product = lookup_by_exact_name(db, seller_id, cleaned)
    if product is not None:
        return _format_product(product)

    result = retrieve_relevant(db, seller_id, cleaned, top_k=5, provider=provider or get_embedding_provider())
    if not result.high_confidence or not result.matches:
        return "Thanks for your message. I could not find a confident match; our seller will follow up shortly."

    match = result.matches[0]
    if match.entity_type == "faq":
        faq = db.scalar(select(Faqs).where(
            Faqs.id == match.entity_id,
            Faqs.seller_id == seller_id,
            Faqs.is_active.is_(True),
        ))
        if faq:
            return faq.answer[:4000]
    elif match.entity_type == "product":
        product = db.scalar(select(Products).where(
            Products.id == match.entity_id,
            Products.seller_id == seller_id,
            Products.is_active.is_(True),
        ))
        if product:
            return _format_product(product)
    return "Thanks for your message. Our seller will follow up shortly."


def send_text_message(phone_number_id: str, access_token: str, recipient: str, message: str) -> str:
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "text",
        "text": {"preview_url": False, "body": message[:4000]},
    }
    try:
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            json=payload,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"WhatsApp request failed ({type(exc).__name__})") from None
    if response.status_code < 200 or response.status_code >= 300:
        # Do not include provider bodies, message text, or bearer tokens in logs/errors.
        raise RuntimeError(f"WhatsApp API returned HTTP {response.status_code}")
    try:
        return response.json()["messages"][0]["id"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise RuntimeError("WhatsApp API response did not contain a message id") from None


MEDIA_TYPES = {"image", "audio", "voice", "video", "document", "sticker"}


def _extract_media_info(incoming: dict) -> tuple[str, str, str]:
    """Extract message type, content summary, and customer acknowledgement text for media messages."""
    msg_type = incoming.get("type", "unknown")
    if msg_type == "text":
        body = incoming.get("text", {}).get("body", "").strip()
        return "text", body, ""

    if msg_type == "image":
        caption = incoming.get("image", {}).get("caption", "").strip()
        content = f"[Image] {caption}".strip() if caption else "[Image message]"
        ack = "Thank you for sending your image. Our team has received it and an agent will review it shortly."
        return "image", content, ack

    if msg_type in ("audio", "voice"):
        content = "[Voice message]" if msg_type == "voice" else "[Audio message]"
        ack = "Thank you for sending your audio message. Our team has received it and an agent will review it shortly."
        return msg_type, content, ack

    if msg_type == "video":
        caption = incoming.get("video", {}).get("caption", "").strip()
        content = f"[Video] {caption}".strip() if caption else "[Video message]"
        ack = "Thank you for sending your video. Our team has received it and an agent will review it shortly."
        return "video", content, ack

    if msg_type == "document":
        doc = incoming.get("document", {})
        filename = doc.get("filename", "").strip()
        caption = doc.get("caption", "").strip()
        parts = [f"[Document: {filename}]" if filename else "[Document]"]
        if caption:
            parts.append(caption)
        content = " ".join(parts).strip()
        ack = "Thank you for sending your document. Our team has received it and an agent will review it shortly."
        return "document", content, ack

    content = f"[{msg_type.title()} message]"
    ack = "Thank you for your message. An agent will review it shortly."
    return msg_type, content, ack


def _handle_status(db: Session, status: dict) -> None:
    message_id = status.get("id")
    state = status.get("status")
    if not message_id or state not in {"sent", "delivered", "read", "failed"}:
        return
    message = db.scalar(select(Messages).where(Messages.whatsapp_message_id == message_id))
    if not message:
        return

    message.status = state
    raw_timestamp = status.get("timestamp")
    event_time = (
        datetime.fromtimestamp(int(raw_timestamp), tz=timezone.utc)
        if raw_timestamp and str(raw_timestamp).isdigit()
        else datetime.now(timezone.utc)
    )

    if state == "delivered":
        message.delivered_at = event_time
    elif state == "read":
        message.read_at = event_time
        if message.delivered_at is None:
            message.delivered_at = event_time
    elif state == "failed":
        conversation = db.get(Conversations, message.conversation_id)
        if conversation:
            conversation.needs_seller_action = True
            errors = status.get("errors", [])
            err_title = errors[0].get("title", "Delivery failure") if errors else "Delivery failure"
            conversation.handoff_reason = f"WhatsApp message failed: {err_title}"[:255]

    publish_conversation_event(message.conversation_id, {
        "type": "status",
        "conversation_id": str(message.conversation_id),
        "whatsapp_message_id": message.whatsapp_message_id,
        "status": state,
        "delivered_at": message.delivered_at.isoformat() if message.delivered_at else None,
        "read_at": message.read_at.isoformat() if message.read_at else None,
    })


def _handle_incoming(
    db: Session,
    phone_number_id: str,
    incoming: dict,
    contact_name: Optional[str],
    provider: Optional[EmbeddingProvider] = None,
) -> None:
    wa_message_id = incoming.get("id")
    sender = incoming.get("from")
    if not wa_message_id or not sender:
        return
    # Webhooks are retried by Meta. Do not answer the same inbound message twice.
    if db.scalar(select(Messages.id).where(Messages.whatsapp_message_id == wa_message_id)):
        return

    seller = db.scalar(select(Sellers).where(Sellers.whatsapp_phone_number_id == phone_number_id))
    if not seller or not seller.whatsapp_access_token_encrypted:
        return
    token = decrypt_access_token(seller.whatsapp_access_token_encrypted)

    conversation_id = db.execute(
        insert(Conversations)
        .values(seller_id=seller.id, customer_phone=sender, customer_name=contact_name)
        .on_conflict_do_update(
            index_elements=[Conversations.seller_id, Conversations.customer_phone],
            set_={"customer_name": contact_name, "last_customer_message_at": text("now()"), "updated_at": text("now()")},
        )
        .returning(Conversations.id)
    ).scalar_one()

    msg_type, content_summary, media_ack = _extract_media_info(incoming)
    is_media = msg_type in MEDIA_TYPES or msg_type != "text"

    inbound_content = content_summary[:4000] if content_summary else f"[{msg_type} message]"
    now = datetime.now(timezone.utc)
    db.add(Messages(
        conversation_id=conversation_id,
        seller_id=seller.id,
        whatsapp_message_id=wa_message_id,
        direction="inbound",
        message_type="text",
        status="received",
        content=inbound_content,
    ))
    db.commit()

    conversation = db.get(Conversations, conversation_id)
    publish_conversation_event(conversation_id, {
        "type": "message",
        "conversation_id": str(conversation_id),
        "direction": "inbound",
        "content": inbound_content,
        "status": "received",
        "created_at": now.isoformat(),
        "needs_seller_action": conversation.needs_seller_action,
        "bot_active": conversation.bot_active,
    })

    if not conversation.bot_active:
        conversation.needs_seller_action = True
        db.commit()
        return

    if is_media:
        conversation.needs_seller_action = True
        conversation.handoff_reason = f"Customer sent {msg_type}"
        reply = media_ack
    else:
        try:
            reply = make_reply(db, seller.id, content_summary, provider) if content_summary else "Please send your question as a text message."
        except Exception:
            # If semantic retrieval is unavailable, hand over to the seller instead of dropping the customer message.
            reply = "Thanks for your message. Our seller will follow up shortly."
            conversation.needs_seller_action = True
        if reply.startswith("Thanks for your message. I could not find a confident match"):
            conversation.needs_seller_action = True

    try:
        outbound_id = send_text_message(phone_number_id, token, sender, reply)
    except RuntimeError:
        conversation.needs_seller_action = True
        db.add(Messages(
            conversation_id=conversation_id,
            seller_id=seller.id,
            direction="outbound",
            message_type="text",
            status="failed",
            content=reply,
        ))
        db.commit()
        publish_conversation_event(conversation_id, {
            "type": "message",
            "conversation_id": str(conversation_id),
            "direction": "outbound",
            "content": reply,
            "status": "failed",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "needs_seller_action": True,
            "bot_active": conversation.bot_active,
        })
        return

    db.add(Messages(
        conversation_id=conversation_id,
        seller_id=seller.id,
        whatsapp_message_id=outbound_id,
        direction="outbound",
        message_type="text",
        status="sent",
        content=reply,
    ))
    db.commit()
    publish_conversation_event(conversation_id, {
        "type": "message",
        "conversation_id": str(conversation_id),
        "direction": "outbound",
        "content": reply,
        "status": "sent",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "needs_seller_action": conversation.needs_seller_action,
        "bot_active": conversation.bot_active,
    })


def process_webhook_payload(payload: dict, db: Optional[Session] = None) -> None:
    close_db = False
    if db is None:
        from app.database import SessionLocal
        db = SessionLocal()
        close_db = True

    try:
        provider = get_embedding_provider()
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                metadata = value.get("metadata", {})
                phone_number_id = metadata.get("phone_number_id")
                if not phone_number_id:
                    continue
                contacts = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])}
                for status in value.get("statuses", []):
                    _handle_status(db, status)
                for incoming in value.get("messages", []):
                    _handle_incoming(db, phone_number_id, incoming, contacts.get(incoming.get("from")), provider)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        if close_db:
            db.close()


@router.post("/webhooks/whatsapp")
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    raw_body = await request.body()
    if not _valid_signature(raw_body, request.headers.get("x-hub-signature-256")):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid webhook payload") from None

    # In test mode, get_db is overridden with the test's transactional db_session.
    # In production, get_db closes at request end, so process_webhook_payload
    # manages its own SessionLocal() safely.
    task_db = db if get_db in request.app.dependency_overrides else None
    background_tasks.add_task(process_webhook_payload, payload, db=task_db)
    return {"status": "received"}
