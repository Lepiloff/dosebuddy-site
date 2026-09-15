#!/usr/bin/env python3
"""Who is stuck, since when, and who stopped being stuck.

Reads `sync.push_no_progress` lines and answers the one question nobody could
answer before redaction 19 shipped: how many real handsets are caught in the
window where a child row is sent ahead of its parent.

    ssh ubuntu@<box> 'docker logs dosebuddy-api 2>&1' | tool/stuck-devices.py

Runs where the log is read rather than on the box: nothing is installed there,
and the log leaves the box already — into a terminal — either way.

**Two counts, not one, and the difference is the whole point.** A device that
appears for three days and then stops has updated: the fix reached it and the
queue drained, which is the mechanism working. A device that has been there
since the first day and has not left is the tail — the population that never
updates, and the only one for whom this trade is a loss. Reporting them as one
number would hide the second inside the first, which is how "how big is the
tail" stayed unanswerable.

Prints no medication or profile names, and cannot: the log line carries
identifiers and counts by construction (see `core/logging.py`), and this reads
only the fields it names below.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict

EVENT = "sync.push_no_progress"


def main() -> int:
    seen: dict[str, list[str]] = defaultdict(list)
    pushes: dict[str, int] = defaultdict(int)
    records: dict[str, int] = defaultdict(int)

    for line in sys.stdin:
        # Any line that is not ours, including docker's own noise, is skipped
        # rather than guessed at.
        start = line.find("{")
        if start < 0 or EVENT not in line:
            continue
        try:
            row = json.loads(line[start:])
        except ValueError:
            continue
        if row.get("event") != EVENT:
            continue
        device = row.get("device_id")
        stamp = row.get("timestamp", "")
        if not device or len(stamp) < 10:
            continue
        seen[device].append(stamp[:10])
        pushes[device] += 1
        records[device] = max(records[device], int(row.get("records") or 0))

    if not seen:
        print(f"no {EVENT} lines in this input — nothing is stuck, or the log "
              "does not go back far enough")
        return 0

    days = sorted({day for stamps in seen.values() for day in stamps})
    last_day = days[-1]

    print(f"{EVENT}: {len(seen)} device(s) over {len(days)} day(s) "
          f"({days[0]} … {last_day})\n")
    print(f"{'device_id':38} {'pushes':>7} {'page':>5}  {'first':10} {'last':10}  state")

    still, healed = 0, 0
    for device, stamps in sorted(seen.items(), key=lambda kv: min(kv[1])):
        first, last = min(stamps), max(stamps)
        if last == last_day:
            state = "still stuck"
            still += 1
        else:
            state = f"stopped after {len(set(stamps))} day(s)"
            healed += 1
        print(f"{device:38} {pushes[device]:>7} {records[device]:>5}  "
              f"{first:10} {last:10}  {state}")

    print(f"\ntail: {still} still stuck on {last_day}; "
          f"{healed} stopped appearing — those updated and drained")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
