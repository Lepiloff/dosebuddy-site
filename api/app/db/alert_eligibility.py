"""Retire caregiver queue debt in the transaction that ends consent.

Publication precedes lifecycle row locks, which precede delivery row locks.
Dispatch holds only the delivery row and never acquires publication afterward.
These triggers can wait for an already started send; its completion precedes
the successful lifecycle acknowledgement. No new sends can begin after it.
"""

RETIRE_CAREGIVER_FUNCTION = """
CREATE OR REPLACE FUNCTION retire_caregiver_deliveries() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'profile_memberships' THEN
        UPDATE alert_deliveries SET state = 'expired'
        WHERE state = 'pending' AND kind IN ('dose_missed', 'profile_stale')
          AND account_id = OLD.account_id AND profile_id = OLD.profile_id;
    ELSIF TG_TABLE_NAME = 'profiles' THEN
        UPDATE alert_deliveries SET state = 'expired'
        WHERE state = 'pending' AND kind IN ('dose_missed', 'profile_stale')
          AND profile_id = OLD.id;
    ELSIF TG_TABLE_NAME = 'accounts' THEN
        UPDATE alert_deliveries SET state = 'expired'
        WHERE state = 'pending' AND kind IN ('dose_missed', 'profile_stale')
          AND (account_id = OLD.id OR profile_id IN (
              SELECT id FROM profiles WHERE owner_account_id = OLD.id
          ));
    END IF;
    RETURN NULL;
END;
$$
"""

# Deleting old revoked membership history must not cancel debt for a later
# live membership of the same account/profile. Only an eligible OLD row ends
# consent. AFTER guards see the actual persisted transition, including LWW.
RETIRE_CAREGIVER_TRIGGERS = (
    ("profile_memberships", "membership_alerts_retire_update", "UPDATE",
     "OLD.revoked_at IS NULL AND OLD.role = 'with_alerts' AND ("
     "NEW.revoked_at IS NOT NULL OR NEW.role <> 'with_alerts' OR "
     "NEW.account_id IS DISTINCT FROM OLD.account_id OR "
     "NEW.profile_id IS DISTINCT FROM OLD.profile_id)"),
    ("profile_memberships", "membership_alerts_retire_delete", "DELETE",
     "OLD.revoked_at IS NULL AND OLD.role = 'with_alerts'"),
    ("profiles", "profile_alerts_retire_delete", "UPDATE",
     "OLD.deleted_at_ms IS NULL AND NEW.deleted_at_ms IS NOT NULL"),
    ("accounts", "account_alerts_retire_delete", "UPDATE",
     "OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL"),
)


def retirement_trigger(table: str, name: str, operation: str, condition: str) -> str:
    return f"""
CREATE TRIGGER {name} AFTER {operation} ON {table}
FOR EACH ROW WHEN ({condition}) EXECUTE FUNCTION retire_caregiver_deliveries()
"""
