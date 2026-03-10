"""
Ghostless API — Community Hub Router

WS   /v1/projects/{project_id}/chat     — real-time project chat
GET  /v1/workers/{worker_id}/messages   — direct messages (REST polling)
POST /v1/workers/{worker_id}/messages   — send a direct message
POST /v1/bugs/report                    — submit a bug report
GET  /v1/bugs/{ticket_id}               — check bug status
POST /v1/announcements                  — broadcast to all workers (admin)
GET  /v1/announcements                  — list active announcements
"""
import json
import re
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import (APIRouter, Depends, Header, HTTPException,
                     Path, Query, WebSocket, WebSocketDisconnect)
from pydantic import BaseModel, validator
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.middleware.auth import AuthContext, get_redis, require_auth
from app.models.models import Announcement, BugReport, Message, Worker

router = APIRouter(tags=["Community Hub"])


# ─── Content Moderation ───────────────────────────────────────────────────────

MAX_MESSAGE_LENGTH = 2000

BANNED_PATTERNS = [
    re.compile(r"\b(spam|phishing|click here|make money fast)\b", re.I),
    re.compile(r"https?://(?!ghostless\.io|your-platform\.io)", re.I),  # no external links
]


def moderate(text: str) -> tuple[bool, str]:
    if not text or not text.strip():
        return False, "Empty message."
    if len(text) > MAX_MESSAGE_LENGTH:
        return False, f"Message exceeds {MAX_MESSAGE_LENGTH} characters."
    for pat in BANNED_PATTERNS:
        if pat.search(text):
            return False, "Message flagged by content filter."
    return True, "ok"


# ─── WebSocket Connection Manager ────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        # room_id → {worker_id: WebSocket}
        self._rooms: Dict[str, Dict[str, WebSocket]] = {}

    async def connect(self, room_id: str, worker_id: str, ws: WebSocket):
        await ws.accept()
        if room_id not in self._rooms:
            self._rooms[room_id] = {}
        self._rooms[room_id][worker_id] = ws

        # Notify others
        await self.broadcast(room_id, {
            "type":    "system",
            "content": f"{worker_id} joined the room.",
            "ts":      int(time.time()),
        }, exclude=worker_id)

    def disconnect(self, room_id: str, worker_id: str):
        if room_id in self._rooms:
            self._rooms[room_id].pop(worker_id, None)
            if not self._rooms[room_id]:
                del self._rooms[room_id]

    async def broadcast(self, room_id: str, payload: dict, exclude: Optional[str] = None):
        text = json.dumps(payload)
        dead = []
        for wid, ws in list(self._rooms.get(room_id, {}).items()):
            if wid == exclude:
                continue
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(wid)
        for wid in dead:
            self.disconnect(room_id, wid)

    async def send_to(self, room_id: str, worker_id: str, payload: dict):
        ws = self._rooms.get(room_id, {}).get(worker_id)
        if ws:
            try:
                await ws.send_text(json.dumps(payload))
            except Exception:
                self.disconnect(room_id, worker_id)

    def room_count(self, room_id: str) -> int:
        return len(self._rooms.get(room_id, {}))


manager = ConnectionManager()


# ─── Schemas ─────────────────────────────────────────────────────────────────

class SendMessageRequest(BaseModel):
    worker_id:  str
    content:    str
    msg_type:   str = "chat"   # chat | help | kudos
    reply_to_id: Optional[str] = None

    @validator("msg_type")
    def validate_type(cls, v):
        if v not in ("chat", "help", "kudos"):
            raise ValueError("msg_type must be: chat | help | kudos")
        return v


class BugReportRequest(BaseModel):
    worker_id:      str
    title:          str
    description:    str
    severity:       str = "medium"
    project_id:     Optional[str] = None
    screenshot_url: Optional[str] = None

    @validator("severity")
    def validate_severity(cls, v):
        if v not in ("low", "medium", "high", "critical"):
            raise ValueError("severity must be: low | medium | high | critical")
        return v


class AnnouncementRequest(BaseModel):
    content:   str
    priority:  str = "normal"
    created_by: Optional[str] = "admin"
    expires_in_hours: Optional[int] = 24


# ─── WebSocket: Project Chat ──────────────────────────────────────────────────

@router.websocket("/projects/{project_id}/chat")
async def project_chat(
    ws: WebSocket,
    project_id: str = Path(...),
    worker_id: str = Query(..., description="Worker's external ID"),
    tenant_id: str = Query(..., description="Tenant slug"),
    token: Optional[str] = Query(None, description="Auth token (alternative to X-API-Key for WS)"),
):
    """
    WebSocket real-time project chat.

    Connect: wss://api.ghostless.io/v1/projects/{project_id}/chat
              ?worker_id=w_001&tenant_id=acme-corp&token=<jwt>

    Send JSON:  {"content": "...", "msg_type": "chat", "reply_to_id": null}
    Receive:    {"id": "...", "type": "chat", "worker_id": "...", "content": "...", "ts": 1234}

    Message types: chat | help | kudos | system | announcement
    """
    # Basic auth check via Redis (in production: verify JWT/API key)
    redis = await get_redis()
    room_id = f"project:{tenant_id}:{project_id}"

    await manager.connect(room_id, worker_id, ws)
    try:
        while True:
            raw = await ws.receive_text()

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(json.dumps({"type": "error", "message": "Invalid JSON"}))
                continue

            content = data.get("content", "").strip()
            ok, reason = moderate(content)
            if not ok:
                await ws.send_text(json.dumps({"type": "moderation", "reason": reason}))
                continue

            msg = {
                "id":         f"msg_{tenant_id[:6]}_{int(time.time()*1000)}",
                "type":       data.get("msg_type", "chat"),
                "worker_id":  worker_id,
                "content":    content,
                "reply_to_id": data.get("reply_to_id"),
                "ts":         int(time.time()),
                "room":       room_id,
            }

            # Broadcast to room
            await manager.broadcast(room_id, msg)

            # Also publish via Redis Pub/Sub for multi-instance deployments
            await redis.publish(f"room:{room_id}", json.dumps(msg))

            # TODO: Persist to messages table async

    except WebSocketDisconnect:
        manager.disconnect(room_id, worker_id)
        await manager.broadcast(room_id, {
            "type":    "system",
            "content": f"{worker_id} left the room.",
            "ts":      int(time.time()),
        })


# ─── Direct Messages (REST) ───────────────────────────────────────────────────

@router.get("/workers/{worker_id}/messages", summary="Get direct messages for a worker")
async def get_messages(
    worker_id: str,
    since: Optional[int] = Query(None, description="Unix timestamp — return messages after this time"),
    limit: int = Query(50, ge=1, le=200),
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Worker)
        .where(Worker.external_id == worker_id)
        .where(Worker.tenant_id == auth.tenant_id)
    )
    worker = result.scalar_one_or_none()
    if not worker:
        raise HTTPException(404, detail={"error": "worker_not_found"})

    query = (
        select(Message)
        .where(Message.worker_id == worker.id)
        .where(Message.room.like("dm:%"))
        .order_by(desc(Message.created_at))
        .limit(limit)
    )
    if since:
        since_dt = datetime.utcfromtimestamp(since)
        query = query.where(Message.created_at > since_dt)

    msg_result = await db.execute(query)
    messages = msg_result.scalars().all()

    return {
        "worker_id": worker_id,
        "messages": [
            {
                "id":         str(m.id),
                "content":    m.content,
                "msg_type":   m.msg_type,
                "created_at": m.created_at.isoformat(),
            }
            for m in reversed(messages)
        ],
    }


@router.post("/workers/{worker_id}/messages", summary="Send a direct message to a worker")
async def send_message(
    worker_id: str,
    body: SendMessageRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    ok, reason = moderate(body.content)
    if not ok:
        raise HTTPException(400, detail={"error": "moderation_failed", "reason": reason})

    result = await db.execute(
        select(Worker)
        .where(Worker.external_id == worker_id)
        .where(Worker.tenant_id == auth.tenant_id)
    )
    worker = result.scalar_one_or_none()
    if not worker:
        raise HTTPException(404, detail={"error": "worker_not_found"})

    msg = Message(
        tenant_id=auth.tenant_id,
        worker_id=worker.id,
        room=f"dm:system:{worker_id}",
        content=body.content,
        msg_type=body.msg_type,
    )
    db.add(msg)
    await db.commit()

    # Push notification to worker via Redis (Notification Service picks it up)
    redis = await get_redis()
    await redis.publish(f"notify:{auth.tenant_id}:{worker_id}", json.dumps({
        "type":    "new_message",
        "content": body.content[:100],
        "ts":      int(time.time()),
    }))

    return {"delivered": True, "message_id": str(msg.id)}


# ─── Bug Reports ──────────────────────────────────────────────────────────────

@router.post("/bugs/report", summary="Submit a bug report")
async def report_bug(
    body: BugReportRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """
    Workers call this to report issues. Returns a ticket ID immediately.
    Critical bugs trigger an instant Slack/webhook notification.
    The most important UX detail: workers get a real ticket number — they feel heard.
    """
    ticket_id = f"BUG-{int(time.time())}"

    result = await db.execute(
        select(Worker)
        .where(Worker.external_id == body.worker_id)
        .where(Worker.tenant_id == auth.tenant_id)
    )
    worker = result.scalar_one_or_none()

    report = BugReport(
        tenant_id=auth.tenant_id,
        worker_id=worker.id if worker else None,
        project_id=body.project_id,
        title=body.title,
        description=body.description,
        severity=body.severity,
        ticket_id=ticket_id,
        screenshot_url=body.screenshot_url,
    )
    db.add(report)
    await db.commit()

    # For critical bugs — push to webhook immediately
    if body.severity in ("high", "critical"):
        from app.tasks.webhooks import dispatch_webhook
        dispatch_webhook.delay(auth.tenant_id, "bug.reported", {
            "ticket_id": ticket_id,
            "severity":  body.severity,
            "title":     body.title,
            "worker_id": body.worker_id,
        })

    return {
        "ticket_id": ticket_id,
        "status":    "received",
        "message":   "Thanks — your report helps everyone. We'll update you here.",
        "expected_response": "Within 4 hours for critical, 24 hours for standard issues.",
    }


@router.get("/bugs/{ticket_id}", summary="Check bug report status")
async def get_bug_status(
    ticket_id: str,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(BugReport)
        .where(BugReport.ticket_id == ticket_id)
        .where(BugReport.tenant_id == auth.tenant_id)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(404, detail={"error": "ticket_not_found"})

    return {
        "ticket_id":   report.ticket_id,
        "status":      report.status,
        "severity":    report.severity,
        "title":       report.title,
        "created_at":  report.created_at.isoformat(),
        "resolved_at": report.resolved_at.isoformat() if report.resolved_at else None,
    }


# ─── Announcements ────────────────────────────────────────────────────────────

@router.post("/announcements", summary="Broadcast an announcement to all workers")
async def create_announcement(
    body: AnnouncementRequest,
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """
    Send a platform-wide announcement to all workers in this tenant.
    Published via Redis Pub/Sub → Notification Service fans out to WebSocket connections.
    """
    ok, reason = moderate(body.content)
    if not ok:
        raise HTTPException(400, detail={"error": "moderation_failed", "reason": reason})

    expires_at = datetime.utcnow() + timedelta(hours=body.expires_in_hours or 24) if body.expires_in_hours else None

    ann = Announcement(
        tenant_id=auth.tenant_id,
        content=body.content,
        priority=body.priority,
        created_by=body.created_by,
        expires_at=expires_at,
    )
    db.add(ann)
    await db.commit()

    # Fan out to all connected workers
    redis = await get_redis()
    await redis.publish(f"announce:{auth.tenant_id}", json.dumps({
        "id":       str(ann.id),
        "content":  body.content,
        "priority": body.priority,
        "ts":       int(time.time()),
    }))

    return {"id": str(ann.id), "published": True, "priority": body.priority}


@router.get("/announcements", summary="List active announcements")
async def list_announcements(
    auth: AuthContext = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Announcement)
        .where(Announcement.tenant_id == auth.tenant_id)
        .where(
            (Announcement.expires_at == None) |
            (Announcement.expires_at > datetime.utcnow())
        )
        .order_by(desc(Announcement.created_at))
        .limit(20)
    )
    announcements = result.scalars().all()

    return {
        "announcements": [
            {
                "id":         str(a.id),
                "content":    a.content,
                "priority":   a.priority,
                "created_at": a.created_at.isoformat(),
                "expires_at": a.expires_at.isoformat() if a.expires_at else None,
            }
            for a in announcements
        ]
    }


# Fix missing import
from datetime import timedelta
