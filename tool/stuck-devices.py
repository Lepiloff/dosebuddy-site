#!/usr/bin/env python3
"""Who is stuck, since when, and who stopped being stuck.

Reads `sync.push_no_progress` lines and answers the one question nobody could
answer before redaction 19 shipped: how many real handsets are caught in the
window where a child row is sent ahead of its parent.

    ssh ubuntu@<box> 'docker logs dosebuddy-api 2>&1' | tool/stuck-devices.py

Runs where the log is read rather than on the box: nothing is installed there,
and the log leaves the box already — into a terminal — either way.

**Two counts, not one, and the difference is the whole point.** A device that
has been there since the first day and has not left is the tail — the
population that never updates, and the only one for whom this trade is a loss.
A device that stops appearing is not that. Reporting them as one number would
hide the second inside the first, which is how "how big is the tail" stayed
unanswerable.

**What stopping does not prove.** It is tempting to read it as "updated, queue
drained" — that is what it means when it means anything good — but a phone that
was uninstalled, switched off, or simply not opened for a week stops appearing
in exactly the same way, because a phone that pushes nothing cannot push a page
that goes nowhere. This prints what was seen and names both readings.

**And `devices.last_seen_at` does not tell them apart**, which this file said
until the app track checked its callers: it is written when the app is *opened*,
from `main()` and from resume, and never by the background task. So a handset
that updated, drained its queue in the background and was never opened — an
elder-mode user confirming doses from the notification is exactly that — shows a
stale `last_seen_at` and reads as abandoned. That is this trade succeeding,
counted as a user lost.

What does tell them apart is whether the device still pushes at all: the
`push_no_progress` lines stop while ordinary requests from the same device
continue. That needs `device_id` on the request log line, which it does not
carry yet — see `docs/debts.md`. Until it does, "stopped appearing" is reported
as the ambiguity it is.

Also absent, deliberately, until it can happen: a device that stops and comes
back is a third thing — updated and stalled again — and that needs 1.4.3 to be
in people's hands first.

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
    # Every line's date, ours or not: it is what makes an empty answer readable.
    # "Nobody is stuck" over a week is a finding; over four hours it is the log
    # being young, and the two look identical without this.
    covered: list[str] = []
    seen: dict[str, list[str]] = defaultdict(list)
    pushes: dict[str, int] = defaultdict(int)
    records: dict[str, int] = defaultdict(int)

    for line in sys.stdin:
        # Any line that is not ours, including docker's own noise, is skipped
        # rather than guessed at.
        start = line.find("{")
        if start < 0:
            continue
        try:
            row = json.loads(line[start:])
        except ValueError:
            continue
        when = row.get("timestamp", "")
        if len(when) >= 10:
            covered.append(when[:10])
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
        if covered:
            span = sorted(set(covered))
            print(f"no {EVENT} lines in {len(span)} day(s) of log "
                  f"({span[0]} … {span[-1]}): nothing was stuck in that window")
        else:
            print(f"no {EVENT} lines, and no dated lines at all — this input is "
                  "not the api log, or the log is empty")
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
            state = f"stopped after {len(set(stamps))} day(s)"  # see below
            healed += 1
        print(f"{device:38} {pushes[device]:>7} {records[device]:>5}  "
              f"{first:10} {last:10}  {state}")

    print(f"\ntail: {still} still stuck on {last_day}; {healed} stopped appearing")
    if healed:
        print("      stopped = updated and drained, OR not pushing at all "
              "(uninstalled, off, unused).\n"
              "      telling them apart needs device_id on the request log "
              "line; it is not there yet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
