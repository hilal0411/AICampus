"""
User-Settings: Name / Passwort ändern, Profilbild verwalten, Account löschen.

- PATCH  /api/auth/settings      → Eigene Einstellungen aktualisieren
- POST   /api/auth/settings/avatar → Profilbild hochladen/ersetzen
- DELETE /api/auth/settings/avatar → Profilbild entfernen
- DELETE /api/auth/settings/account → Eigene Account löschen (Username-Bestätigung)
"""

import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlmodel import Session

from database.base import get_session
from database.models import User, GlobalUserRole
from services import media_service, user_service
from services.auth_service import (
    get_current_user,
    hash_password,
    verify_password,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth/settings", tags=["User-Settings"])


class SettingsUpdate(BaseModel):
    current_password: str = Field(default="", max_length=128)
    new_name: str = Field(default="", max_length=200)
    new_password: str = Field(default="", max_length=128)


class DeleteAccountRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)


class ProfileUpdate(BaseModel):
    profile: str = Field(default="", max_length=50000)


@router.patch("/")
async def update_settings(
    data: SettingsUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """
    Eigene Einstellungen aktualisieren.

    - `new_name` → Name ändern (kein Passwort nötig)
    - `new_password` → Passwort ändern (aktuelles Passwort als Bestätigung nötig)
    """
    name_changed = False
    password_changed = False

    # ─── Name ändern ───────────────────────────────────────────
    if data.new_name:
        stripped = data.new_name.strip()
        if not stripped:
            raise HTTPException(400, "Name darf nicht leer sein.")
        if stripped != user.name:
            user.name = stripped
            name_changed = True

    # ─── Passwort ändern ───────────────────────────────────────
    if data.new_password:
        if not data.current_password:
            raise HTTPException(
                400,
                "Aktuelles Password ist erforderlich, um das Password zu ändern.",
            )

        # LDAP-User haben keinen DB-Password-Hash
        if not user.password_hash:
            raise HTTPException(
                400,
                "Dein Account verwendet LDAP-Authentifizierung. "
                "Passwortänderungen sind hier nicht möglich.",
            )

        if not verify_password(data.current_password, user.password_hash):
            raise HTTPException(
                401,
                "Das aktuelle Password ist falsch.",
            )

        if data.new_password != data.current_password:
            user.password_hash = hash_password(data.new_password)
            password_changed = True

    if not name_changed and not password_changed:
        return {"message": "Keine Änderungen vorgenommen."}

    session.add(user)
    session.commit()
    session.refresh(user)

    messages = []
    if name_changed:
        messages.append("Name wurde aktualisiert.")
    if password_changed:
        messages.append("Password wurde geändert.")

    display_role = "Admin" if user.role == GlobalUserRole.ADMIN else "User"
    return {
        "message": " ".join(messages),
        "user": _user_dict(user, display_role),
    }


@router.patch("/profile")
async def update_profile(
    data: ProfileUpdate,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Eigenen Steckbrief (Markdown) speichern — öffentlich unter /u/<username>."""
    text = data.profile.strip()
    user.profile = text or None
    session.add(user)
    session.commit()
    session.refresh(user)
    return {"message": "Steckbrief wurde gespeichert."}


def _user_dict(user: User, display_role: str) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "name": user.name,
        "role": display_role,
        "avatar": f"/avatars/{user.avatar.rsplit('/', 1)[-1]}" if user.avatar else None,
    }


@router.post("/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Profilbild hochladen (PNG/JPG/WebP/GIF, max. 2 MB). Ersetzt ein bestehendes Bild."""
    data = await file.read()
    rel_path, _mime = media_service.save_avatar(file.filename or "avatar.png", data)

    if user.avatar:
        media_service.delete_avatar_file(user.avatar)

    user.avatar = rel_path
    session.add(user)
    session.commit()
    session.refresh(user)

    display_role = "Admin" if user.role == GlobalUserRole.ADMIN else "User"
    return {"message": "Profilbild wurde aktualisiert.", "user": _user_dict(user, display_role)}


@router.delete("/avatar")
async def delete_avatar(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Profilbild entfernen."""
    if not user.avatar:
        return {"message": "Kein Profilbild vorhanden.", "user": _user_dict(user, "Admin" if user.role == GlobalUserRole.ADMIN else "User")}

    media_service.delete_avatar_file(user.avatar)
    user.avatar = None
    session.add(user)
    session.commit()
    session.refresh(user)

    display_role = "Admin" if user.role == GlobalUserRole.ADMIN else "User"
    return {"message": "Profilbild wurde entfernt.", "user": _user_dict(user, display_role)}


@router.delete("/account")
async def delete_account(
    data: DeleteAccountRequest,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """
    Eigenen Account löschen (Soft Delete + Anonymisierung, DSGVO-konform).

    Persönliche Daten (Foren-Posts, Einreichungen, Tokens, …) werden
    endgültig gelöscht, der Account wird anonymisiert. Von dem User
    erstellte Kurse und Kurs-Inhalte bleiben erhalten (Institutsdaten).

    Sicherheit: Username muss als Bestätigung mitgeschickt werden.
    Admin-Accounts können nicht selbst gelöscht werden (letzter-Admin-
    Lockout — im Admin-Bereich von einem anderen Admin möglich).
    """
    if user.role == GlobalUserRole.ADMIN:
        raise HTTPException(
            403,
            "Admin-Accounts können nicht selbst gelöscht werden. "
            "Bitte lass deinen Account von einem anderen Admin entfernen.",
        )

    if data.username.strip() != user.username:
        raise HTTPException(
            400,
            "Der eingegebene Username stimmt nicht mit deinem Account überein.",
        )

    username = user_service.soft_delete_user(session, user)
    logger.info("[Account] User '%s' hat seinen Account selbst gelöscht.", username)
    return {
        "message": (
            "Dein Account wurde anonymisiert und deine persönlichen Daten gelöscht. "
            "Von dir erstellte Kurse und Kurs-Inhalte bleiben erhalten."
        )
    }