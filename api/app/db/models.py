"""Server tables.

Two kinds live here.

What the device never had — accounts, devices, sessions, membership, pairing.
And the mirror of the device model — profiles, medications, schedules,
dose_events, stock_events — whose fields are the device's, because the device is
the source of truth (spec §0.5) and the server repeats rather than redefines.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
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

    # Client-generated, and stable across app *updates* — the id lives in the
    # device's DataStore, which an update leaves alone. Deleting the app or
    # clearing its data destroys it, and a new id then means a new device with
    # its own session.
    #
    # A backup never carries it: the app excludes DataStore from Android backup
    # and device transfer on purpose, because an id restored onto a second
    # handset would have this table count one device where there are two — and
    # the alarms for a profile are armed by whichever device this table names.
    #
    # Said precisely because the earlier wording — "a reinstall wipes the id" —
    # made an update look like it produced a new device, which had us reading
    # the wrong row during acceptance.
    #
    # Measured 2026-08-28: two handsets that had been reinstalled between the
    # 25th and the 28th appeared as two new rows, and the id one of them
    # reported matched none of the three the account already held. The id does
    # not survive that, as written.
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

    # How far this device's cursor has reached. The cursor itself belongs to the
    # client and always will — this is a copy of what it was last handed, not
    # the authority on where it is.
    #
    # It exists for one question the server previously could not answer: has the
    # device that just took over a profile actually seen that it did? Without
    # it, the nudge telling the previous phone to stop went out immediately,
    # and — since the new phone only learns by pulling — the usual result was a
    # gap where neither phone rang. Measured 18.08: 2 min 36 s of silence, where
    # the unfixed build had produced 28 s of both ringing.
    #
    # Silence is the worse of the two (spec invariant 1), so the previous phone
    # is now kept ringing until this column says the new one is ready. Null
    # means "not known to have pulled anything", which is the cautious reading:
    # it holds the nudge rather than releasing it.
    cursor_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

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

    # BIGINT, not INTEGER. An Android colour is unsigned ARGB — 0xFF2A9D8F is
    # 4283215696, past the top of int32 — and SQLite stores it happily on the
    # device. Synthetic zeros in the tests hid this; the first profile with a
    # colour would have failed the whole push.
    color: Mapped[int] = mapped_column(BigInteger, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    # is_elder_mode is deliberately absent. It is a property of a device that
    # happens to sit on the profile row for historical reasons, and syncing it
    # would push one phone's large-text UI onto every device on the account.

    # Milliseconds, like every other mirrored table. These began as timestamptz
    # — profiles were built for pairing, before sync existed — and that made
    # last-write-wins compare a device clock against a database type. The
    # comparison would not have failed loudly; it would have picked the wrong
    # winner.
    created_at_ms: Mapped[int] = mapped_column(BigInteger)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger)
    deleted_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Same write identity as every other mirrored table; see SyncMixin.
    origin_device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    op_seq: Mapped[int] = mapped_column(BigInteger, default=0)

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
        # One live membership per (profile, account). Revoking a caregiver
        # keeps the row as history: who could see what, and when, is a GDPR
        # question, so the uniqueness is partial rather than the rows being
        # deleted.
        #
        # Deleting the account is the exception, and it outranks this — the
        # `account_id` cascade takes the rows with it. Erasure is the stronger
        # obligation, and a record of who watched whom is not history worth
        # keeping about someone who asked to be forgotten. Said here because
        # `delete_account` briefly tried to hold both rules at once, with an
        # UPDATE the cascade then undid.
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

    # How long after a dose is reported missed before the watcher hears about
    # it. On the link rather than global: one caregiver wants to know at once,
    # another only if it is really being forgotten, and both are right.
    dose_alert_after_minutes: Mapped[int] = mapped_column(Integer, default=30)

    # How long a profile's device may stay silent before that silence is itself
    # reported.
    #
    # This was 12 hours, which a phone in Doze reaches most nights without
    # anything being wrong — the caregiver would have been woken most mornings,
    # and an alert that is usually false is one the caregiver learns to swipe
    # away before reading. 36 hours sits out a night, a weekend at a relative's
    # and a day with no signal, and still catches a phone that is genuinely off
    # or lost within two of its owner's mornings.
    #
    # It says the app has not been in touch, and nothing more: a missed dose is
    # `dose_missed` and only ever comes from the device saying so. Silence is not
    # evidence of a missed dose, and the wording on the device must not imply it.
    stale_alert_after_hours: Mapped[int] = mapped_column(Integer, default=36)

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


# ---------------------------------------------------------------------------
# The mirror of the device model
#
# Fields are the device's, taken from lib/core/db/tables.dart. The device is the
# source of truth (spec §0.5) and the server repeats it — including the parts
# that look odd here, like times kept as JSON text, because reformatting them
# would mean a second implementation of something the device already decided.
#
# Two decisions worth stating, because both look like mistakes otherwise.
#
# **Timestamps are BIGINT milliseconds, not timestamptz.** That is what the
# device stores and what crosses the wire. Converting in and out of a database
# type buys nothing and is exactly where an hour goes missing at a DST boundary.
#
# **Enum-valued columns are TEXT, not Postgres enums.** A Postgres enum would
# reject a value it does not know — and the value that arrives tomorrow comes
# from a newer client, relayed through here to an older one. The server must
# store what it is given; deciding whether a value is understood is the
# client's job, which is why the app track deliberately left CHECK off these
# columns too (05d §3).
# ---------------------------------------------------------------------------


class SyncMixin:
    """The v1.0 sync-ready foundation (spec §4.4), plus the identity of the write.

    `updated_at` alone cannot order writes. Two edits to one row inside a single
    millisecond are ordinary — a tap and its follow-up — and a rule that treats
    equal timestamps as the same write drops the second one in silence. It also
    cannot break a tie between two devices deterministically.

    So every write carries who made it and its position in that device's own
    monotonic sequence, which owes nothing to a clock. The comparison
    (updated_at, origin_device_id, op_seq) then does two jobs at once: it orders
    writes, and it makes a resend an exact tie — recognised as the repeat it is,
    rather than applied again.
    """

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    created_at_ms: Mapped[int] = mapped_column(BigInteger)
    updated_at_ms: Mapped[int] = mapped_column(BigInteger)
    deleted_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Taken from the authenticated caller, never from the body: a device must
    # not be able to write under another device's identity.
    origin_device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    op_seq: Mapped[int] = mapped_column(BigInteger, default=0)


class Medication(Base, SyncMixin):
    __tablename__ = "medications"

    # (key, server_seq), not two separate indexes: pull filters on the key and
    # orders by the sequence in one go, and an index that serves only half of
    # that leaves Postgres discarding rows or sorting them. See 0007.
    __table_args__ = (Index("ix_medications_profile_id_server_seq", "profile_id", "server_seq"),)

    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(EncryptedString)
    notes: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)
    dosage_text: Mapped[str | None] = mapped_column(EncryptedString, nullable=True)

    dose_amount: Mapped[float] = mapped_column(Float, default=1)
    form: Mapped[str] = mapped_column(String(32))
    pack_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    refill_threshold_days: Mapped[int] = mapped_column(Integer, default=3)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # The device's photo_path is a local path and never crosses (contract §4.5);
    # this is the S3 key it maps to.
    photo_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # current_stock is deliberately absent: it is a cache of the stock journal
    # sum, and a derived value on the wire is a second source of truth.

    server_seq: Mapped[int] = mapped_column(
        BigInteger, server_default=text(f"nextval('{SERVER_SEQ}')"), index=True
    )


class Schedule(Base, SyncMixin):
    __tablename__ = "schedules"

    __table_args__ = (
        Index("ix_schedules_medication_id_server_seq", "medication_id", "server_seq"),
    )

    medication_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("medications.id", ondelete="CASCADE")
    )
    type: Mapped[str] = mapped_column(String(32))

    # Stored and returned verbatim. The server never expands a schedule into
    # doses — the device does (spec §3.4), and a second calendar implementation
    # would disagree with the first at a DST boundary, quietly.
    times: Mapped[str] = mapped_column(Text)
    days_of_week: Mapped[str | None] = mapped_column(Text, nullable=True)
    interval_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_date: Mapped[str] = mapped_column(String(10))
    end_date: Mapped[str | None] = mapped_column(String(10), nullable=True)

    server_seq: Mapped[int] = mapped_column(
        BigInteger, server_default=text(f"nextval('{SERVER_SEQ}')"), index=True
    )


class DoseEvent(Base, SyncMixin):
    __tablename__ = "dose_events"

    __table_args__ = (
        Index("ix_dose_events_profile_id_server_seq", "profile_id", "server_seq"),
    )

    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("schedules.id", ondelete="SET NULL"), nullable=True
    )
    medication_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("medications.id", ondelete="CASCADE"), index=True
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE")
    )

    planned_at_ms: Mapped[int] = mapped_column(BigInteger, index=True)

    # Not monotonic: missed can return to pending when the intake window is
    # widened, and taken can return to pending from the calendar. Nothing here
    # may assume it only moves forward (05d §1.1а).
    status: Mapped[str] = mapped_column(String(32), index=True)

    action_at_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    snooze_count: Mapped[int] = mapped_column(Integer, default=0)
    snoozed_until_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dose_amount: Mapped[float] = mapped_column(Float)

    server_seq: Mapped[int] = mapped_column(
        BigInteger, server_default=text(f"nextval('{SERVER_SEQ}')"), index=True
    )


class StockEvent(Base, SyncMixin):
    __tablename__ = "stock_events"

    __table_args__ = (
        Index("ix_stock_events_medication_id_server_seq", "medication_id", "server_seq"),
    )

    medication_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("medications.id", ondelete="CASCADE")
    )
    delta: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(String(32))
    dose_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dose_events.id", ondelete="SET NULL"), nullable=True
    )

    server_seq: Mapped[int] = mapped_column(
        BigInteger, server_default=text(f"nextval('{SERVER_SEQ}')"), index=True
    )


class AlertKind(str, enum.Enum):
    """Two signals, and collapsing them would be a lie in one direction or the
    other (contract §5).

    `dose_missed` means the profile's device reported a dose as missed.
    `profile_stale` means that device has said nothing for too long — which is
    not the same thing and must not be dressed up as it. Alerting on silence
    cries wolf; staying quiet lets a real miss pass unnoticed exactly when the
    phone is off, which is the case worth worrying about.
    """

    dose_missed = "dose_missed"
    profile_stale = "profile_stale"

    # Not a warning to a caregiver at all: a nudge to one of the owner's own
    # devices that it no longer arms the alarms for a profile. It rides here for
    # what the table already provides — retries, backoff, a collapse key, a TTL,
    # and a row afterwards saying whether it went. It also keeps the FCM key in
    # the worker, which is the only process that has it.
    #
    # Two things about it differ from the alerts, and both are load-bearing:
    # it names a single device rather than an account, and its payload is built
    # at send time rather than stored. See services/alerts.resolve_nudge.
    reminder_authority_lost = "reminder_authority_lost"


class AlertState(str, enum.Enum):
    """Where an alert has got to.

    `sent_at` alone could not express this. It was written before the send was
    attempted, so a row saying "delivered" was the only trace of an alert that
    FCM had refused — and after the fact there was no way to even find the ones
    that had been lost.
    """

    pending = "pending"
    sent = "sent"
    # Attempts exhausted. The alert is lost, but visibly so.
    given_up = "given_up"
    # Outlived its usefulness before it could be delivered. Telling a caregiver
    # at noon about a dose missed at breakfast is help; telling them on Thursday
    # about Monday is noise they cannot act on.
    expired = "expired"


class AlertDelivery(Base):
    """One row per alert raised, carrying how far it has got.

    In the database rather than in Redis on purpose: Redis is allowed to lose
    its contents, and the cost of losing this is a caregiver woken again for
    something they already saw. It doubles as the record of what we told whom,
    which is the sort of question that gets asked after the fact.

    The unique index still makes an alert unrepeatable, so detection can run
    every minute and raise the same alert every time without consequence. What
    changed is that claiming it no longer counts as delivering it.
    """

    __tablename__ = "alert_deliveries"
    __table_args__ = (
        # `profile_id` is part of the key, and leaving it out silently cost
        # people. For `profile_stale` the subject is a date, so without it one
        # caregiver looking after two parents received a single alert a day
        # between them — the second parent's phone could be dead for a week and
        # never be mentioned. The more profiles someone watches, the more the
        # key hid, which is exactly backwards.
        Index(
            "uq_alert_once",
            "account_id",
            "profile_id",
            "kind",
            "subject_id",
            unique=True,
        ),
        # The loop's only query: what is due now. Ordered so the index answers
        # it without reading rows that are waiting out their backoff.
        Index("ix_alert_deliveries_due", "state", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[AlertKind] = mapped_column(Enum(AlertKind, name="alert_kind"))

    # Which device to tell, when the answer is not "every device the account
    # has". Null for the two caregiver alerts, and it must stay null for them:
    # reaching either of someone's two phones is telling them, and narrowing
    # that to one device would lose alerts to a phone left at home.
    #
    # Set only for `reminder_authority_lost`, where the opposite holds. The
    # message concerns exactly one device, and sending it to the whole account
    # would deliver "you no longer ring for this profile" to the device that
    # now does — correct only because the receiver would throw it away, which
    # is a guarantee borrowed from the other side of the wire.
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=True
    )

    # The dose for dose_missed; for profile_stale, the window it belongs to, so
    # a device offline for a week produces one alert a day rather than one every
    # time the scan runs.
    subject_id: Mapped[str] = mapped_column(String(64))

    state: Mapped[str] = mapped_column(String(32), default=AlertState.pending.value)
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    # When to try next. Set to now on creation, pushed out by backoff on each
    # failure, so one query serves both the first attempt and every retry.
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    # After this the alert is no longer worth delivering; see AlertState.expired.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Why the last attempt failed. Kept because "the alerts stopped arriving" is
    # not a question that can be answered from an empty table.
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Null until something was actually delivered. It used to be written before
    # the attempt, which made it a record of intent wearing the name of a fact.
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
