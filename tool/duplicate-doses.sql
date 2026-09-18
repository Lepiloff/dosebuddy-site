-- How many doses exist twice, and how many of those matter.
--
--     ssh -i <key> ubuntu@<box> \
--       'docker exec -i dosebuddy-postgres psql -U $POSTGRES_USER -d $POSTGRES_DB' \
--       < ~/Projects/own/dosebuddy-site/tool/duplicate-doses.sql
--
-- Read-only, and deliberately written against `schedule_id + planned_at_ms`
-- rather than against `dose_key`: the column ships in migration 0019 and the
-- box is frozen until the stuck-devices window closes, so a query needing it
-- could not be run until then. This one answers today.
--
-- **What it is for.** A handover leaves both phones materialising the same
-- scheduled dose for a while, each under its own uuid, and the app track's
-- local UNIQUE quarantines whichever arrives second. Every path out of that —
-- merging the rows, teaching the new client to adopt the other id, or deciding
-- to live with it — costs the client track real work, and the cost is worth
-- arguing about only if the pairs exist. Nobody has counted them.
--
-- **Counts only.** No names, no ids, no timestamps finer than a day. This runs
-- against the live database of a medication app; a query that answers a design
-- question has no business also carrying out who takes what.
--
-- Three numbers matter, and the second is the one to read first:
--
--   pairs        keys with more than one row, including harmless ones
--   alive        keys with more than one row *not* soft-deleted — the defect
--   disagreeing  alive pairs whose rows are not in the same state, so that the
--                quarantined one is hiding something the other does not say
--
-- A pair where one row is soft-deleted is ordinary and expected: changing a
-- schedule puts out the old event and creates a new one (app track), and if the
-- time did not move, both land on the same key. Only `alive` is the failure.

\pset footer off

WITH twins AS (
    SELECT schedule_id,
           planned_at_ms,
           count(*)                                          AS rows,
           count(*) FILTER (WHERE deleted_at_ms IS NULL)      AS alive,
           count(DISTINCT status) FILTER (WHERE deleted_at_ms IS NULL) AS states,
           min(created_at_ms)                                 AS first_ms,
           max(created_at_ms)                                 AS last_ms
    FROM dose_events
    WHERE schedule_id IS NOT NULL
    GROUP BY schedule_id, planned_at_ms
    HAVING count(*) > 1
)
SELECT count(*)                                        AS pairs,
       count(*) FILTER (WHERE alive > 1)               AS alive,
       count(*) FILTER (WHERE alive > 1 AND states > 1) AS disagreeing,
       coalesce(max(rows), 0)                          AS worst,
       to_char(to_timestamp(min(first_ms) / 1000), 'YYYY-MM-DD') AS earliest,
       to_char(to_timestamp(max(last_ms) / 1000), 'YYYY-MM-DD')  AS latest
FROM twins;

-- And what is attached to them. A dose confirmed on both phones is decremented
-- from the packet twice, which no merge of the dose rows alone would undo.
WITH twins AS (
    SELECT schedule_id, planned_at_ms
    FROM dose_events
    WHERE schedule_id IS NOT NULL AND deleted_at_ms IS NULL
    GROUP BY schedule_id, planned_at_ms
    HAVING count(*) > 1
)
SELECT count(*) AS stock_events_on_twinned_doses
FROM stock_events s
JOIN dose_events d ON d.id = s.dose_event_id
JOIN twins t ON t.schedule_id = d.schedule_id AND t.planned_at_ms = d.planned_at_ms
WHERE s.deleted_at_ms IS NULL;
