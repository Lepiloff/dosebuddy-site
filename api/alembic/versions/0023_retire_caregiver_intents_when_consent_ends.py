"""Caregiver queue retirement at membership/profile/account lifecycle commit.

Stop previous API/worker processes before upgrade; old workers do not recheck
consent at dispatch. Queue retirement and trigger installation are atomic.
"""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

# Frozen definitions, independent of future production helper changes.
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

TRIGGERS = (
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


def upgrade() -> None:
    op.execute("SELECT sync_publication_lock()")
    op.execute(RETIRE_CAREGIVER_FUNCTION)
    for table, name, operation, condition in TRIGGERS:
        op.execute(f"""
            CREATE TRIGGER {name} AFTER {operation} ON {table}
            FOR EACH ROW WHEN ({condition}) EXECUTE FUNCTION retire_caregiver_deliveries()
        """)
    # Retain unique intents for replay/regrant, including debt that is not due
    # yet. Authority nudges have separate eligibility and remain untouched.
    op.execute("""
        UPDATE alert_deliveries AS delivery SET state = 'expired'
        WHERE state = 'pending' AND kind IN ('dose_missed', 'profile_stale')
          AND NOT EXISTS (
              SELECT 1 FROM profile_memberships AS membership
              JOIN profiles AS profile ON profile.id = membership.profile_id
              JOIN accounts AS recipient ON recipient.id = membership.account_id
              JOIN accounts AS owner ON owner.id = profile.owner_account_id
              WHERE membership.account_id = delivery.account_id
                AND membership.profile_id = delivery.profile_id
                AND membership.role = 'with_alerts' AND membership.revoked_at IS NULL
                AND profile.deleted_at_ms IS NULL
                AND recipient.deleted_at IS NULL AND owner.deleted_at IS NULL
          )
    """)


def downgrade() -> None:
    op.execute("SELECT sync_publication_lock()")
    for table, name, _, _ in TRIGGERS:
        op.execute(f"DROP TRIGGER {name} ON {table}")
    op.execute("DROP FUNCTION retire_caregiver_deliveries()")
    # Cancelled debt stays retired; a downgrade must not resurrect consent.
