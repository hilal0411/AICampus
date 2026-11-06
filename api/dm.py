"""
Messaging: plattform-weite Direktnachrichten + Gruppenchats (kursübergreifend).

Jeder eingeloggte User kann mit jedem chatten. Gruppenchats verwaltet
(umbenennen, Mitglieder) nur der Ersteller; jeder kann den Chat verlassen.
Kein Löschen von Nachrichten (bewusst, s. docs/LLM-plans/messaging.md).

Architektur-Basis: Kurs-Forum (Polling ?after_id= + SSE-Trigger + Read-State
+ Ungelesen-Zähler). Realtime über den bestehenden Notification-Stream
(Event "dm_message", Payload ohne Inhalt).

Chats:
- GET    /api/dm/chats               (eigene Chats + Ungelesen + letzte Nachricht)
- POST   /api/dm/chats               (1:1-Chat starten; bestehender wird wiederverwendet)
- POST   /api/dm/chats/group         (Gruppen-Chat anlegen)
- PATCH  /api/dm/chats/{id}          (umbenennen, nur Ersteller)
- POST   /api/dm/chats/{id}/members  (Mitglied hinzufügen, nur Ersteller)
- DELETE /api/dm/chats/{id}/members/{user_id} (entfernen; sich selbst = verlassen)

Nachrichten (im Chat):
- GET    /api/dm/chats/{id}/messages?after_id=N  (Polling)
- POST   /api/dm/chats/{id}/messages
- POST   /api/dm/chats/{id}/read?up_to_id=N

User-Suche (für „Neuer Chat"):
- GET    /api/users/search?q=
"""

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text, update
from sqlmodel import Session, func, select

from database.base import get_session
from database.models import (
    DmChat,
    DmChatCreate,
    DmChatMember,
    DmChatReadState,
    DmChatUpdate,
    DmGroupChatCreate,
    DmMessage,
    DmMessageCreate,
    Notification,
    User,
)
from services import notifications
from services.auth_service import get_current_user
from services.timeutil import campus_date, utc_iso

router = APIRouter(prefix="/api", tags=["Messages"])

DM_PAGE_SIZE = 200


# ─── Helpers ───────────────────────────────────────────────────────


def _get_chat_or_404(session: Session, chat_id: int) -> DmChat:
    chat = session.get(DmChat, chat_id)
    if not chat:
        raise HTTPException(404, "Chat nicht gefunden.")
    return chat


def _require_membership(session: Session, chat: DmChat, viewer: User) -> None:
    if not session.exec(
        select(DmChatMember)
        .where(DmChatMember.chat_id == chat.id)
        .where(DmChatMember.user_id == viewer.id)
    ).first():
        raise HTTPException(404, "Chat nicht gefunden.")


def chat_member_map(session: Session, chat_ids: list[int]) -> dict[int, list[int]]:
    """chat_id → [user_id, …] in einem Query (ohne N+1)."""
    if not chat_ids:
        return {}
    rows = session.exec(
        select(DmChatMember.chat_id, DmChatMember.user_id)
        .where(DmChatMember.chat_id.in_(chat_ids))  # type: ignore[attr-defined]
    ).all()
    out: dict[int, list[int]] = {}
    for chat_id, user_id in rows:
        out.setdefault(chat_id, []).append(user_id)
    return out


def _users_by_id(session: Session, user_ids: set[int]) -> dict[int, User]:
    if not user_ids:
        return {}
    return {
        u.id: u
        for u in session.exec(select(User).where(User.id.in_(user_ids))).all()  # type: ignore[attr-defined]
    }


def chat_title(session: Session, chat: DmChat, viewer: User,
               members: list[int], users: dict[int, User]) -> str:
    """Titel aus der Sicht des Viewers: Gruppenname oder Name des Gegenübers."""
    if chat.name:
        return chat.name
    others = [u for u in (users.get(uid) for uid in members) if u and u.id != viewer.id]
    return others[0].name if others else "Chat"


def _chat_dict(
    session: Session,
    chat: DmChat,
    viewer: User,
    members: list[int],
    users: dict[int, User],
    last_msg: Optional[DmMessage],
    unread_count: int,
) -> dict[str, Any]:
    is_group = len(members) > 2
    other_id = next((uid for uid in members if uid != viewer.id), None)
    other = users.get(other_id) if other_id else None
    last_sender = users.get(last_msg.user_id) if last_msg else None
    return {
        "id": chat.id,
        "name": chat_title(session, chat, viewer, members, users),
        "is_group": is_group,
        "created_by": chat.created_by,
        "can_manage": chat.created_by == viewer.id,
        "member_count": len(members),
        "members": [
            {
                "id": uid,
                "username": u.username if u else "unknown",
                "name": u.name if u else "unknown",
                "avatar": f"/avatars/{u.avatar.rsplit('/', 1)[-1]}" if u and u.avatar else None,
            }
            for uid, u in ((uid, users.get(uid)) for uid in members)
        ],
        "other": {
            "id": other.id,
            "username": other.username,
            "name": other.name,
            "avatar": f"/avatars/{other.avatar.rsplit('/', 1)[-1]}" if other and other.avatar else None,
        } if other and not is_group else None,
        "last_message": (
            {
                "id": last_msg.id,
                "content": last_msg.content,
                "user_id": last_msg.user_id,
                "name": last_sender.name if last_sender else "unknown",
                "created_at": utc_iso(last_msg.created_at),
                "label": campus_date(last_msg.created_at),
            }
            if last_msg
            else None
        ),
        "unread_count": unread_count,
    }


def load_unread_counts(session: Session, user_id: int) -> dict[int, int]:
    """Ungelesene Nachrichten pro Chat (eine Query über alle eigenen Chats).

    Eigene Nachrichten zählen nie als ungelesen (man hat sie ja geschickt) —
    sonst würde der eigene Senden-Trigger (SSE) vor dem Read-State-Commit
    kurzzeitig ein rotes Badge auslösen.
    """
    sql = """
        SELECT m.chat_id, COUNT(*) AS cnt
        FROM dm_messages AS m
        LEFT JOIN dm_chat_read_state AS rs
          ON rs.user_id = :user_id AND rs.chat_id = m.chat_id
        WHERE m.user_id != :user_id AND m.id > COALESCE(rs.last_read_message_id, 0)
        GROUP BY m.chat_id
    """
    rows = session.execute(text(sql).bindparams(user_id=user_id)).all()
    return {chat_id: cnt for chat_id, cnt in rows}


def load_chats_payload(session: Session, viewer: User) -> list[dict[str, Any]]:
    """Alle Chats des Users, zuletzt aktiver zuerst (ohne N+1)."""
    chats = session.exec(
        select(DmChat)
        .join(DmChatMember, DmChatMember.chat_id == DmChat.id)
        .where(DmChatMember.user_id == viewer.id)
    ).all()
    if not chats:
        return []

    chat_ids = [c.id for c in chats]  # type: ignore[misc]
    members = chat_member_map(session, chat_ids)

    # 1:1-Chats ohne Nachrichten sind für beide Seiten unsichtbar — der Chat
    # „existiert" erst ab der ersten Nachricht (sonst bekäme das Gegenüber
    # einen leeren Chat bloß fürs Reinklicken). Gruppen bleiben sichtbar.
    last_ids = dict(
        session.execute(
            text("SELECT chat_id, MAX(id) AS mid FROM dm_messages GROUP BY chat_id")
        ).all()
    )
    chats = [
        c for c in chats
        if not (len(members.get(c.id, [])) == 2 and c.id not in last_ids)
    ]

    last_msgs = {}
    for cid in chat_ids:
        mid = last_ids.get(cid)
        if mid:
            m = session.get(DmMessage, mid)
            if m:
                last_msgs[cid] = m

    user_ids = {m.user_id for m in last_msgs.values()} | {
        uid for ids in members.values() for uid in ids
    }
    users = _users_by_id(session, user_ids)
    unread = load_unread_counts(session, viewer.id)

    def sort_key(chat: DmChat):
        last = last_msgs.get(chat.id)  # type: ignore[arg-type]
        return last.created_at if last else chat.created_at

    return [
        _chat_dict(session, c, viewer, members.get(c.id, []), users,
                   last_msgs.get(c.id), unread.get(c.id, 0))  # type: ignore[arg-type]
        for c in sorted(chats, key=sort_key, reverse=True)
    ]


def _chat_dict_for(session: Session, chat: DmChat, viewer: User) -> dict[str, Any]:
    """Einzelnes Chat-Dict mit frischen Member-, Last-Msg- und Ungelesen-Daten."""
    members = notifications.chat_member_ids(session, chat.id)
    users = _users_by_id(session, set(members))
    last_id = latest_message_id(session, chat.id)
    last = session.get(DmMessage, last_id) if last_id else None
    unread = load_unread_counts(session, viewer.id).get(chat.id, 0)  # type: ignore[arg-type]
    return _chat_dict(session, chat, viewer, members, users, last, unread)


def _find_one_on_one(session: Session, a_id: int, b_id: int) -> Optional[DmChat]:
    """Bestehenden 1:1-Chat zwischen zwei Users finden (sonst None)."""
    row = session.execute(
        text(
            """
            SELECT c.id FROM dm_chats AS c
            JOIN dm_chat_members AS ma ON ma.chat_id = c.id AND ma.user_id = :a
            JOIN dm_chat_members AS mb ON mb.chat_id = c.id AND mb.user_id = :b
            WHERE c.name IS NULL
            """
        ).bindparams(a=a_id, b=b_id)
    ).first()
    if not row:
        return None
    chat = session.get(DmChat, row[0])
    if chat and len(notifications.chat_member_ids(session, chat.id)) == 2:
        return chat
    return None


def _ensure_member(session: Session, chat: DmChat, user_id: int) -> None:
    if not session.exec(
        select(DmChatMember)
        .where(DmChatMember.chat_id == chat.id)
        .where(DmChatMember.user_id == user_id)
    ).first():
        session.add(DmChatMember(chat_id=chat.id, user_id=user_id))  # type: ignore[arg-type]


def _delete_chat(session: Session, chat: DmChat) -> None:
    """Chat inkl. Nachrichten, Read-States + DM-Benachrichtigungen löschen."""
    for m in session.exec(select(DmMessage).where(DmMessage.chat_id == chat.id)).all():
        session.delete(m)
    for rs in session.exec(select(DmChatReadState).where(DmChatReadState.chat_id == chat.id)).all():
        session.delete(rs)
    for note in session.exec(select(Notification).where(Notification.chat_id == chat.id)).all():
        session.delete(note)
    for mem in session.exec(select(DmChatMember).where(DmChatMember.chat_id == chat.id)).all():
        session.delete(mem)
    session.delete(chat)


def _message_dict(msg: DmMessage, sender: Optional[User], viewer: User) -> dict[str, Any]:
    return {
        "id": msg.id,
        "chat_id": msg.chat_id,
        "content": msg.content,
        "created_at": utc_iso(msg.created_at),
        "user_id": msg.user_id,
        "username": sender.username if sender else "unknown",
        "name": sender.name if sender else "unknown",
        "avatar": f"/avatars/{sender.avatar.rsplit('/', 1)[-1]}" if sender and sender.avatar else None,
        "is_self": msg.user_id == viewer.id,
    }


def load_messages_payload(
    session: Session,
    chat: DmChat,
    viewer: User,
    after_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Nachrichten des Chats chronologisch + Sender-Infos (ohne N+1).

    after_id: None → letzte DM_PAGE_SIZE (Initial-Load),
              sonst nur Nachrichten mit id > after_id (Polling).
    """
    msgs = session.exec(
        select(DmMessage)
        .where(DmMessage.chat_id == chat.id)
        .order_by(DmMessage.id.desc())  # type: ignore[attr-defined]
    ).all()
    msgs = list(reversed(msgs))
    if after_id is not None:
        msgs = [m for m in msgs if m.id > after_id]
    else:
        msgs = msgs[-DM_PAGE_SIZE:]

    users = _users_by_id(session, {m.user_id for m in msgs})
    return [_message_dict(m, users.get(m.user_id), viewer) for m in msgs]


def latest_message_id(session: Session, chat_id: int) -> int:
    last = session.exec(
        select(func.max(DmMessage.id)).where(DmMessage.chat_id == chat_id)  # type: ignore[call-overload]
    ).one()
    return last or 0


def advance_read_state(session: Session, user_id: int, chat_id: int, target: int) -> None:
    """Gelesen-Stand eines Chats vorrücken (nie zurück). Commit beim Aufrufer."""
    if target <= 0:
        return
    rs = session.exec(
        select(DmChatReadState)
        .where(DmChatReadState.user_id == user_id)
        .where(DmChatReadState.chat_id == chat_id)
    ).first()
    if rs is None:
        session.add(
            DmChatReadState(
                chat_id=chat_id,  # type: ignore[arg-type]
                user_id=user_id,  # type: ignore[arg-type]
                last_read_message_id=target,
            )
        )
    elif rs.last_read_message_id < target:
        rs.last_read_message_id = target
        session.add(rs)


def _mark_chat_notifications_read(session: Session, viewer: User, chat_id: int) -> None:
    """Auto-Read: eigene dm_message-Benachrichtigungen des Chats als gelesen.

    Wer den Chat aufruft, sieht die Nachrichten ja im Chat (wie im Forum).
    """
    try:
        result = session.exec(
            update(Notification)
            .where(Notification.user_id == viewer.id)
            .where(Notification.type == "dm_message")
            .where(Notification.chat_id == chat_id)
            .where(Notification.read_at.is_(None))
            .values(read_at=datetime.now())
        )
        session.commit()
        if result.rowcount:  # Glocke sofort updaten (alle Tabs), statt Poll abzuwarten
            notifications.notify_read(viewer.id)  # type: ignore[arg-type]
    except Exception:
        session.rollback()


# ─── Chats ─────────────────────────────────────────────────────────


@router.get("/dm/chats")
async def list_dm_chats(
    session: Session = Depends(get_session),
    viewer: User = Depends(get_current_user),
):
    """Alle eigenen Chats (zuletzt aktiver zuerst) + Ungelesen-Zähler."""
    return load_chats_payload(session, viewer)


@router.post("/dm/chats", status_code=201)
async def start_one_on_one_chat(
    data: DmChatCreate,
    session: Session = Depends(get_session),
    viewer: User = Depends(get_current_user),
):
    """1:1-Chat mit einem User starten (bestehender wird wiederverwendet)."""
    target = session.get(User, data.member_id)
    if not target:
        raise HTTPException(404, "User nicht gefunden.")
    if target.id == viewer.id:
        raise HTTPException(400, "Mit sich selbst kann man nicht chatten.")

    chat = _find_one_on_one(session, viewer.id, target.id)
    if chat is None:
        chat = DmChat(name=None, created_by=viewer.id)  # type: ignore[arg-type]
        session.add(chat)
        session.commit()
        session.refresh(chat)
        _ensure_member(session, chat, viewer.id)
        _ensure_member(session, chat, target.id)
        session.commit()

    return _chat_dict_for(session, chat, viewer)


@router.post("/dm/chats/group", status_code=201)
async def create_group_chat(
    data: DmGroupChatCreate,
    session: Session = Depends(get_session),
    viewer: User = Depends(get_current_user),
):
    """Gruppen-Chat anlegen (Ersteller wird automatisch Mitglied)."""
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "Gruppen-Name darf nicht leer sein.")

    member_ids = sorted({int(uid) for uid in data.member_ids} | {viewer.id})
    if len(member_ids) < 2:
        raise HTTPException(400, "Eine Gruppe braucht mindestens dich und eine weitere Person.")
    users = _users_by_id(session, set(member_ids))
    missing = [uid for uid in member_ids if uid not in users]
    if missing:
        raise HTTPException(404, "Ein oder mehrere User existieren nicht.")

    chat = DmChat(name=name, created_by=viewer.id)  # type: ignore[arg-type]
    session.add(chat)
    session.commit()
    session.refresh(chat)
    for uid in member_ids:
        _ensure_member(session, chat, uid)
    session.commit()

    return _chat_dict_for(session, chat, viewer)


@router.patch("/dm/chats/{chat_id}")
async def rename_chat(
    chat_id: int,
    data: DmChatUpdate,
    session: Session = Depends(get_session),
    viewer: User = Depends(get_current_user),
):
    """Chat umbenennen (nur der Ersteller)."""
    chat = _get_chat_or_404(session, chat_id)
    _require_membership(session, chat, viewer)
    if chat.created_by != viewer.id:
        raise HTTPException(403, "Nur der Ersteller kann den Chat umbenennen.")

    if data.name is not None:
        name = data.name.strip()
        if not name:
            raise HTTPException(400, "Name darf nicht leer sein.")
        chat.name = name
    session.add(chat)
    session.commit()
    session.refresh(chat)
    return {"ok": True, "name": chat.name}


# ─── Mitglieder ────────────────────────────────────────────────────


@router.post("/dm/chats/{chat_id}/members")
async def add_chat_member(
    chat_id: int,
    body: dict[str, int],
    session: Session = Depends(get_session),
    viewer: User = Depends(get_current_user),
):
    """Mitglied hinzufügen (nur der Ersteller)."""
    chat = _get_chat_or_404(session, chat_id)
    _require_membership(session, chat, viewer)
    if chat.created_by != viewer.id:
        raise HTTPException(403, "Nur der Ersteller kann Mitglieder hinzufügen.")

    target = session.get(User, body.get("user_id") or 0)
    if not target:
        raise HTTPException(404, "User nicht gefunden.")
    if target.id == viewer.id:
        raise HTTPException(400, "Du bist bereits Mitglied.")

    existing = session.exec(
        select(DmChatMember)
        .where(DmChatMember.chat_id == chat.id)
        .where(DmChatMember.user_id == target.id)
    ).first()
    if existing:
        raise HTTPException(409, "User ist bereits Mitglied.")

    _ensure_member(session, chat, target.id)
    session.commit()
    return {"ok": True}


@router.delete("/dm/chats/{chat_id}/members/{user_id}")
async def remove_chat_member(
    chat_id: int,
    user_id: int,
    session: Session = Depends(get_session),
    viewer: User = Depends(get_current_user),
):
    """Mitglied entfernen (nur Ersteller); sich selbst = Chat verlassen.

    Verlässt der Ersteller eine Gruppe, an der noch jemand hängen bleibt,
    wandert die Ersteller-Rolle auf das älteste verbleibende Mitglied über.
    Sinkt die Mitgliederzahl auf unter 2, wird der Chat gelöscht
    (Nachrichten und Read-States mit).
    """
    chat = _get_chat_or_404(session, chat_id)
    _require_membership(session, chat, viewer)

    if user_id != viewer.id and chat.created_by != viewer.id:
        raise HTTPException(403, "Nur der Ersteller kann Mitglieder entfernen.")

    row = session.exec(
        select(DmChatMember)
        .where(DmChatMember.chat_id == chat.id)
        .where(DmChatMember.user_id == user_id)
    ).first()
    if not row:
        raise HTTPException(404, "User ist nicht Mitglied.")
    session.delete(row)

    remaining = [uid for uid in notifications.chat_member_ids(session, chat.id) if uid != user_id]
    if len(remaining) < 2:
        _delete_chat(session, chat)
    elif chat.created_by == user_id:
        # Ersteller-Rolle auf das älteste verbleibende Mitglied übertragen
        earliest = session.exec(
            select(DmChatMember)
            .where(DmChatMember.chat_id == chat.id)
            .order_by(DmChatMember.joined_at, DmChatMember.id)  # type: ignore[arg-type]
        ).first()
        if earliest:
            chat.created_by = earliest.user_id  # type: ignore[arg-type]
    session.commit()
    return {"ok": True, "deleted": len(remaining) < 2}


# ─── Nachrichten (pro Chat) ────────────────────────────────────────


@router.get("/dm/chats/{chat_id}/messages")
async def list_dm_messages(
    chat_id: int,
    after_id: Optional[int] = Query(None, ge=0, description="Nur Nachrichten neuer als diese ID (Polling)"),
    session: Session = Depends(get_session),
    viewer: User = Depends(get_current_user),
):
    """Nachrichten des Chats (chronologisch). Auto-Read der DM-Benachrichtigungen."""
    chat = _get_chat_or_404(session, chat_id)
    _require_membership(session, chat, viewer)
    payload = load_messages_payload(session, chat, viewer, after_id)
    _mark_chat_notifications_read(session, viewer, chat.id)
    return payload


@router.post("/dm/chats/{chat_id}/messages", status_code=201)
async def create_dm_message(
    chat_id: int,
    data: DmMessageCreate,
    session: Session = Depends(get_session),
    viewer: User = Depends(get_current_user),
):
    """Nachricht in den Chat schreiben."""
    chat = _get_chat_or_404(session, chat_id)
    _require_membership(session, chat, viewer)
    content = data.content.strip()
    if not content:
        raise HTTPException(400, "Nachricht darf nicht leer sein.")

    msg = DmMessage(chat_id=chat.id, user_id=viewer.id, content=content)  # type: ignore[arg-type]
    session.add(msg)
    session.commit()
    session.refresh(msg)

    # Live: offene DM-Panels sofort updaten (statt 4-s-Poll abzuwarten).
    # Kein Glocke-Eintrag: DMs leben im Sprechblasen-Badge, Doppel-Meldung
    # war nur Lärm (entschieden 2026-11-06).
    notifications.push_dm_message(session, chat.id, msg.id)  # type: ignore[arg-type]

    return _message_dict(msg, viewer, viewer)


@router.post("/dm/chats/{chat_id}/read")
async def mark_chat_read(
    chat_id: int,
    up_to_id: Optional[int] = Query(None, ge=1, description="Bis zu dieser Nachricht-ID gelesen"),
    session: Session = Depends(get_session),
    viewer: User = Depends(get_current_user),
):
    """Chat als gelesen markieren (Stand rückt nur vor, nie zurück)."""
    chat = _get_chat_or_404(session, chat_id)
    _require_membership(session, chat, viewer)

    target = up_to_id if up_to_id is not None else latest_message_id(session, chat.id)
    if target > 0:
        advance_read_state(session, viewer.id, chat.id, target)
        session.commit()
    return {"ok": True}


# ─── User-Suche (für „Neuer Chat") ─────────────────────────────────


@router.get("/users/search")
async def search_users(
    q: str = Query("", max_length=100),
    session: Session = Depends(get_session),
    viewer: User = Depends(get_current_user),
):
    """Globale User-Suche (username/name/email) für neue Chats, max. 10 Treffer."""
    users = session.exec(select(User)).all()
    if q:
        q_lower = q.lower()
        users = [
            u for u in users
            if q_lower in u.username.lower()
            or q_lower in u.name.lower()
            or q_lower in (u.email or "").lower()
        ]
    results = [
        {
            "id": u.id,
            "username": u.username,
            "name": u.name,
            "avatar": f"/avatars/{u.avatar.rsplit('/', 1)[-1]}" if u.avatar else None,
        }
        for u in users
        if u.id != viewer.id
    ][:10]
    return {"users": results}
