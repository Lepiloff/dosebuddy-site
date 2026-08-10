"""Server tables.

Two kinds live here. The mirror of the device model comes later, with the sync
endpoints; what exists now is what the device never had — accounts, devices,
sessions, membership and pairing.

`profiles` is the exception: pairing is about sharing a profile, so the row has
to exist before pairing can be built. Its fields are the device's, because the
device is the source of truth (spec §0.5) and the server repeats it.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.crypto import EncryptedString
from app.db.session import Base

SERVER_SEQ = "server_seq"


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, enum.Enum):
    """Role lives on the (account ↔ profile) link, never on an account or a
    device (spec §1.2). One account can own one profile and watch another at the
    same time, which a global mode could not express."""

    owner = "owner"
    viewer = "viewer"
    with_alerts = "with_alerts"


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)

    # Identity is the Google subject, not the email. Emails change; `sub` does
    # not, and keying on email would one day read as "I lost access to my data".
    google_sub: Mapped[str] = mapped_column(String(255), unique=True, index=True)

    # Kept for support, never used as a key — which is why encrypting it costs
    # nothing: random nonces make the column unqueryable, and nothing queries it.
    email: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    devices: Mapped[list[Device]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class Device(Base):
    __tablename__ = "devices"

    # Client-generated and stable. A new id means a new device with its own
    # session, not a reinstall — a reinstall wipes app storage and the id with it.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )

    platform: Mapped[str] = mapped_column(String(32))
    app_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    push_token: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Feeds `profile_stale` (contract §5). Without it, "no data" cannot be told
    # apart from "nothing to report", and the two mean opposite things.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    account: Mapped[Account] = relationship(back_populates="devices")


class RefreshToken(Base):
    """Opaque and stored hashed, so revocation is real.

    A self-contained JWT cannot be withdrawn, and "remove this caregiver" or
    "unlink this device" have to take effect immediately rather than whenever
    the token happens to expire.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )

    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Set when this token is rotated away. Presenting a token that already has a
    # successor means the token leaked and both copies are in play, so the whole
    # chain for the device is revoked rather than just this one.
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    owner_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )

    # The reminder owner (spec §1.4): exactly one device materialises alarms for
    # this profile. Nullable because a profile can exist before its owning
    # device has claimed authority.
    owner_device_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )

    name: Mapped[str] = mapped_column(EncryptedString)
    color: Mapped[int] = mapped_column(Integer, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    # is_elder_mode is deliberately absent. It is a property of a device that
    # happens to sit on the profile row for historical reasons, and syncing it
    # would push one phone's large-text UI onto every device on the account.

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Cursor ordering. Server-assigned, never the device's clock: phone clocks
    # run backwards, and equal milliseconds at a page boundary lose rows
    # silently and forever.
    server_seq: Mapped[int] = mapped_column(
        BigInteger, server_default=text(f"nextval('{SERVER_SEQ}')"), index=True
    )

    memberships: Mapped[list[ProfileMembership]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )


class ProfileMembership(Base):
    __tablename__ = "profile_memberships"
    __table_args__ = (
        # One live membership per (profile, account). Revoked rows are kept as
        # history: who could see what, and when, is a GDPR question, so the
        # uniqueness is partial rather than the rows being deleted.
        Index(
            "uq_membership_live",
            "profile_id",
            "account_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), index=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[Role] = mapped_column(Enum(Role, name="role"))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    profile: Mapped[Profile] = relationship(back_populates="memberships")


class PairingCode(Base):
    """A short code, and the record that consent was given.

    The owner of the profile issues it, so the person whose health data is being
    shared performs the act of sharing — the cleanest consent story under GDPR.
    The row is kept after redemption for accountability, not for debugging.
    """

    __tablename__ = "pairing_codes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), index=True
    )
    issued_by_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE")
    )

    # The role is fixed when the code is issued, not when it is entered:
    # otherwise the receiving side would decide how much it gets to see.
    role: Mapped[Role] = mapped_column(Enum(Role, name="role"))

    # HMAC, not a bare hash. The code is short enough to brute-force offline
    # from a leaked table; the server secret makes the table alone useless.
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    redeemed_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
