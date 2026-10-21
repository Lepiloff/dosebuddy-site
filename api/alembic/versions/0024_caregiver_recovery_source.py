"""Additive caregiver recovery source; coverage and reader default disabled.

Definitions are frozen here. No legacy backfill or automatic enable.
"""
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

SCHEMA = (
    """CREATE SEQUENCE caregiver_event_seq INCREMENT 1 CACHE 1 NO CYCLE""",
    """CREATE TABLE caregiver_recovery_control (
 id INTEGER PRIMARY KEY, producer_enabled BOOLEAN DEFAULT false NOT NULL,
 reader_enabled BOOLEAN DEFAULT false NOT NULL,
 committed_frontier BIGINT DEFAULT 0 NOT NULL, epoch UUID,
 coverage_started_at TIMESTAMPTZ, closed_at TIMESTAMPTZ,
 CONSTRAINT ck_caregiver_recovery_frontier CHECK (committed_frontier >= 0),
 CONSTRAINT ck_caregiver_recovery_singleton CHECK (id = 1),
 CONSTRAINT ck_caregiver_recovery_coverage CHECK (NOT producer_enabled OR (epoch IS NOT NULL AND coverage_started_at IS NOT NULL)),
 CONSTRAINT ck_caregiver_recovery_reader CHECK (NOT reader_enabled OR producer_enabled))""",
    """CREATE TABLE caregiver_recovery_events (
 delivery_id UUID PRIMARY KEY REFERENCES alert_deliveries(id) ON DELETE CASCADE,
 event_seq BIGINT NOT NULL, epoch UUID NOT NULL, raised_at TIMESTAMPTZ NOT NULL,
 expires_at TIMESTAMPTZ NOT NULL, retired_at TIMESTAMPTZ,
 CONSTRAINT ck_caregiver_recovery_seq CHECK (event_seq > 0),
 CONSTRAINT ck_caregiver_recovery_lifetime CHECK (expires_at > raised_at))""",
    """CREATE UNIQUE INDEX uq_caregiver_recovery_seq ON caregiver_recovery_events(event_seq)""",
    """CREATE INDEX ix_caregiver_recovery_epoch_seq ON caregiver_recovery_events(epoch,event_seq)""",
    """CREATE INDEX ix_caregiver_recovery_expiry ON caregiver_recovery_events(expires_at)""",
)

FUNCTIONS = (
    """CREATE OR REPLACE FUNCTION next_caregiver_event_seq() RETURNS bigint
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM sync_publication_lock();
    RETURN nextval('caregiver_event_seq');
END;
$$""",
    """CREATE OR REPLACE FUNCTION project_caregiver_event() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE active_epoch uuid; allocated_seq bigint;
BEGIN
    IF NEW.kind NOT IN ('dose_missed', 'profile_stale') THEN RETURN NULL; END IF;
    SELECT epoch INTO active_epoch FROM caregiver_recovery_control
    WHERE id = 1 AND producer_enabled;
    IF active_epoch IS NULL THEN RETURN NULL; END IF;
    allocated_seq := next_caregiver_event_seq();
    INSERT INTO caregiver_recovery_events
        (delivery_id, event_seq, epoch, raised_at, expires_at)
    VALUES (NEW.id, allocated_seq, active_epoch,
            NEW.next_attempt_at, NEW.expires_at);
    UPDATE caregiver_recovery_control SET committed_frontier = allocated_seq WHERE id = 1;
    RETURN NULL;
END;
$$""",
    """CREATE OR REPLACE FUNCTION retire_caregiver_recovery() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'profile_memberships' THEN
        UPDATE caregiver_recovery_events AS recovery SET retired_at = CURRENT_TIMESTAMP
        FROM alert_deliveries AS delivery
        WHERE recovery.delivery_id = delivery.id AND recovery.retired_at IS NULL
          AND delivery.account_id = OLD.account_id AND delivery.profile_id = OLD.profile_id;
    ELSIF TG_TABLE_NAME = 'profiles' THEN
        UPDATE caregiver_recovery_events AS recovery SET retired_at = CURRENT_TIMESTAMP
        FROM alert_deliveries AS delivery
        WHERE recovery.delivery_id = delivery.id AND recovery.retired_at IS NULL
          AND delivery.profile_id = OLD.id;
    ELSIF TG_TABLE_NAME = 'accounts' THEN
        UPDATE caregiver_recovery_events AS recovery SET retired_at = CURRENT_TIMESTAMP
        FROM alert_deliveries AS delivery
        WHERE recovery.delivery_id = delivery.id AND recovery.retired_at IS NULL
          AND (delivery.account_id = OLD.id OR delivery.profile_id IN (
              SELECT id FROM profiles WHERE owner_account_id = OLD.id
          ));
    END IF;
    RETURN NULL;
END;
$$""",
    """CREATE OR REPLACE FUNCTION guard_caregiver_recovery() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF ROW(NEW.delivery_id, NEW.event_seq, NEW.epoch, NEW.raised_at, NEW.expires_at)
       IS DISTINCT FROM
       ROW(OLD.delivery_id, OLD.event_seq, OLD.epoch, OLD.raised_at, OLD.expires_at)
       OR (OLD.retired_at IS NOT NULL AND NEW.retired_at IS DISTINCT FROM OLD.retired_at) THEN
        RAISE EXCEPTION 'caregiver recovery identity, lifetime and retirement are immutable'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$""",
)

TRIGGERS = (
    ('alert_deliveries', 'alert_deliveries_recovery_publication', """CREATE TRIGGER alert_deliveries_recovery_publication BEFORE INSERT ON alert_deliveries
FOR EACH STATEMENT EXECUTE FUNCTION sync_publication_fence()"""),
    ('alert_deliveries', 'alert_deliveries_recovery_project', """CREATE TRIGGER alert_deliveries_recovery_project AFTER INSERT ON alert_deliveries
FOR EACH ROW EXECUTE FUNCTION project_caregiver_event()"""),
    ('caregiver_recovery_control', 'caregiver_recovery_control_publication', """CREATE TRIGGER caregiver_recovery_control_publication
BEFORE INSERT OR UPDATE OR DELETE ON caregiver_recovery_control
FOR EACH STATEMENT EXECUTE FUNCTION sync_publication_fence()"""),
    ('caregiver_recovery_events', 'caregiver_recovery_events_immutable', """CREATE TRIGGER caregiver_recovery_events_immutable BEFORE UPDATE ON caregiver_recovery_events
FOR EACH ROW EXECUTE FUNCTION guard_caregiver_recovery()"""),
    ('profile_memberships', 'membership_recovery_retire_update', """CREATE TRIGGER membership_recovery_retire_update AFTER UPDATE ON profile_memberships
FOR EACH ROW WHEN (OLD.revoked_at IS NULL AND OLD.role = 'with_alerts' AND (NEW.revoked_at IS NOT NULL OR NEW.role <> 'with_alerts' OR NEW.account_id IS DISTINCT FROM OLD.account_id OR NEW.profile_id IS DISTINCT FROM OLD.profile_id)) EXECUTE FUNCTION retire_caregiver_recovery()"""),
    ('profile_memberships', 'membership_recovery_retire_delete', """CREATE TRIGGER membership_recovery_retire_delete AFTER DELETE ON profile_memberships
FOR EACH ROW WHEN (OLD.revoked_at IS NULL AND OLD.role = 'with_alerts') EXECUTE FUNCTION retire_caregiver_recovery()"""),
    ('profiles', 'profile_recovery_retire_delete', """CREATE TRIGGER profile_recovery_retire_delete AFTER UPDATE ON profiles
FOR EACH ROW WHEN (OLD.deleted_at_ms IS NULL AND NEW.deleted_at_ms IS NOT NULL) EXECUTE FUNCTION retire_caregiver_recovery()"""),
    ('accounts', 'account_recovery_retire_delete', """CREATE TRIGGER account_recovery_retire_delete AFTER UPDATE ON accounts
FOR EACH ROW WHEN (OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL) EXECUTE FUNCTION retire_caregiver_recovery()"""),
)

SEED = """INSERT INTO caregiver_recovery_control (id, producer_enabled, reader_enabled)
VALUES (1, false, false) ON CONFLICT (id) DO NOTHING"""


def upgrade() -> None:
    op.execute("SELECT sync_publication_lock()")
    for statement in (*SCHEMA, *FUNCTIONS, *(definition for _, _, definition in TRIGGERS), SEED):
        op.execute(statement)


def downgrade() -> None:
    op.execute("SELECT sync_publication_lock()")
    for table, name, _ in reversed(TRIGGERS):
        op.execute(f"DROP TRIGGER {name} ON {table}")
    for name in ("guard_caregiver_recovery", "retire_caregiver_recovery",
                 "project_caregiver_event", "next_caregiver_event_seq"):
        op.execute(f"DROP FUNCTION {name}()")
    op.execute("DROP TABLE caregiver_recovery_events")
    op.execute("DROP TABLE caregiver_recovery_control")
    op.execute("DROP SEQUENCE caregiver_event_seq")
