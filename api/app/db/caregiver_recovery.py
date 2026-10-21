"""Database source and irreversible consent fences for the recovery projection."""

from app.db.alert_eligibility import RETIRE_CAREGIVER_TRIGGERS

NEXT_CAREGIVER_EVENT_SEQ_FUNCTION = """
CREATE OR REPLACE FUNCTION next_caregiver_event_seq() RETURNS bigint
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM sync_publication_lock();
    RETURN nextval('caregiver_event_seq');
END;
$$
"""

PROJECT_CAREGIVER_EVENT_FUNCTION = """
CREATE OR REPLACE FUNCTION project_caregiver_event() RETURNS trigger
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
$$
"""

RETIRE_CAREGIVER_RECOVERY_FUNCTION = """
CREATE OR REPLACE FUNCTION retire_caregiver_recovery() RETURNS trigger
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
$$
"""

GUARD_CAREGIVER_RECOVERY_FUNCTION = """
CREATE OR REPLACE FUNCTION guard_caregiver_recovery() RETURNS trigger
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
$$
"""

RECOVERY_FUNCTIONS = (
    NEXT_CAREGIVER_EVENT_SEQ_FUNCTION, PROJECT_CAREGIVER_EVENT_FUNCTION,
    RETIRE_CAREGIVER_RECOVERY_FUNCTION, GUARD_CAREGIVER_RECOVERY_FUNCTION,
)

# INSERT only: delivery state updates holding a row across FCM must never take
# publication. The statement fence also protects ORM/alternative queue writers.
RECOVERY_TRIGGERS = (
    ("alert_deliveries", "alert_deliveries_recovery_publication", """
CREATE TRIGGER alert_deliveries_recovery_publication BEFORE INSERT ON alert_deliveries
FOR EACH STATEMENT EXECUTE FUNCTION sync_publication_fence()
"""),
    ("alert_deliveries", "alert_deliveries_recovery_project", """
CREATE TRIGGER alert_deliveries_recovery_project AFTER INSERT ON alert_deliveries
FOR EACH ROW EXECUTE FUNCTION project_caregiver_event()
"""),
    ("caregiver_recovery_control", "caregiver_recovery_control_publication", """
CREATE TRIGGER caregiver_recovery_control_publication
BEFORE INSERT OR UPDATE OR DELETE ON caregiver_recovery_control
FOR EACH STATEMENT EXECUTE FUNCTION sync_publication_fence()
"""),
    ("caregiver_recovery_events", "caregiver_recovery_events_immutable", """
CREATE TRIGGER caregiver_recovery_events_immutable BEFORE UPDATE ON caregiver_recovery_events
FOR EACH ROW EXECUTE FUNCTION guard_caregiver_recovery()
"""),
) + tuple(
    (table, name.replace("alerts_retire", "recovery_retire"), f"""
CREATE TRIGGER {name.replace('alerts_retire', 'recovery_retire')} AFTER {operation} ON {table}
FOR EACH ROW WHEN ({condition}) EXECUTE FUNCTION retire_caregiver_recovery()
""")
    for table, name, operation, condition in RETIRE_CAREGIVER_TRIGGERS
)

SEED_RECOVERY_CONTROL = """
INSERT INTO caregiver_recovery_control (id, producer_enabled, reader_enabled)
VALUES (1, false, false) ON CONFLICT (id) DO NOTHING
"""
