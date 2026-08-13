"""Wire shapes for sync.

Field names are the contract's, which are the device's. Where the column here
is named differently — `updated_at_ms` against the wire's `updated_at` — the
alias lives in one place rather than being remembered at each call site.

Every float is declared `allow_inf_nan=False`, and that is not belt-and-braces.
The app track has to reject NaN when deserialising because SQLite stores it as
NULL and NULL is meaningful in these columns — a NaN would silently switch stock
tracking off. The same reasoning applies harder here: this server is the relay,
so a NaN accepted from one buggy client is a NaN handed to every other device on
the account. Rejecting at the boundary where data enters the shared store is
cheaper than every reader defending itself.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field


class SyncBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: uuid.UUID
    created_at: int = Field(alias="created_at")
    updated_at: int
    deleted_at: int | None = None

    # The device's own monotonic counter for this write. Not a clock, so two
    # edits inside one millisecond are still distinguishable, and a resend is an
    # exact repeat rather than a new write that happens to look identical.
    op_seq: int = Field(ge=0)


class ProfileIn(SyncBase):
    name: str = Field(max_length=200)
    color: int = 0
    sort_order: int = 0
    # is_elder_mode is not on the wire: it describes a device, not a profile.


class MedicationIn(SyncBase):
    profile_id: uuid.UUID
    name: str = Field(max_length=200)
    notes: str | None = Field(default=None, max_length=2000)
    dosage_text: str | None = Field(default=None, max_length=200)
    dose_amount: float = Field(default=1, allow_inf_nan=False)
    form: str = Field(max_length=32)
    pack_size: float | None = Field(default=None, allow_inf_nan=False)
    refill_threshold_days: int = 3
    is_active: bool = True
    photo_key: str | None = Field(default=None, max_length=255)
    # current_stock is not on the wire: it is a cache of the stock journal sum.


class ScheduleIn(SyncBase):
    medication_id: uuid.UUID
    type: str = Field(max_length=32)
    # Verbatim. The server stores and returns these without parsing them.
    times: str
    days_of_week: str | None = None
    interval_days: int | None = None
    start_date: str = Field(max_length=10)
    end_date: str | None = Field(default=None, max_length=10)


class DoseEventIn(SyncBase):
    schedule_id: uuid.UUID | None = None
    medication_id: uuid.UUID
    profile_id: uuid.UUID
    planned_at: int
    status: str = Field(max_length=32)
    action_at: int | None = None
    snooze_count: int = 0
    snoozed_until: int | None = None
    dose_amount: float = Field(allow_inf_nan=False)


class StockEventIn(SyncBase):
    medication_id: uuid.UUID
    delta: float = Field(allow_inf_nan=False)
    reason: str = Field(max_length=32)
    dose_event_id: uuid.UUID | None = None


class Changes(BaseModel):
    model_config = ConfigDict(extra="ignore")

    profiles: list[ProfileIn] = []
    medications: list[MedicationIn] = []
    schedules: list[ScheduleIn] = []
    dose_events: list[DoseEventIn] = []
    stock_events: list[StockEventIn] = []


class PushIn(BaseModel):
    changes: Changes = Changes()


class Outcome(BaseModel):
    id: uuid.UUID
    entity: str
    code: str


class PushOut(BaseModel):
    """Three outcomes per record, expressed as two lists.

    Anything absent from both was applied — the common case, and the one not
    worth a line each in a batch of a thousand.

    `rejected` is final: sending it again changes nothing, and the client should
    stop and surface it. `retry` is not the client's fault — a parent that has
    not arrived yet, a conflict — and the same record sent later will succeed.
    Collapsing the two would make a client either give up on something
    recoverable or loop forever on something that never will be.
    """

    cursor: str
    rejected: list[Outcome] = []
    retry: list[Outcome] = []


class PullOut(BaseModel):
    cursor: str
    has_more: bool
    changes: dict[str, list[dict]]

    # Every profile the caller can see, and their role on it. Present on every
    # response, not only when something changed.
    #
    # The client has to know, for each row, whether it belongs to a profile they
    # own or one they merely watch — the two go to different places, and getting
    # it wrong arms an alarm on the wrong phone (spec §1.4). The profile row
    # carries `role`, but it may not be in the same page: order is by server_seq,
    # so a profile last touched months ago sorts far behind doses from this
    # morning. Deriving the answer from the shape of a row instead — "no
    # schedule_id, so it must be watched" — reads a field that is legitimately
    # null on an owned row too.
    #
    # A map, so the answer is always there and never depends on arrival order.
    # It is small: a family has a handful of profiles, not thousands.
    roles: dict[str, str] = {}
