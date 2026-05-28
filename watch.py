#!/usr/bin/env python3
"""
watch.py - cron/timer wrapper around reconcile.py  (HANDOFF §6.1).

Now that reconcile reaches a clean baseline (3 MISSING, all EXPECTED-OFF;
0 undoc / 0 failed / 0 unreachable), running it on a schedule turns drift into
a *timeline*. The raw snapshots ARE the trouble history; the one-line log is the
at-a-glance signal of WHEN something deviated and WHEN it cleared.

Each run:
  1. run reconcile.py --json  -> a timestamped snapshot under HISTORY/
  2. refresh the canonical drift.json (so ask.py reads near-fresh w/o --probe)
  3. classify CLEAN vs DRIFT using the SAME EXPECTED-OFF rule as ask.py
     (a MISSING service whose lifecycle is on-demand/stream-only is NOT a fault)
  4. diff significant findings vs the previous snapshot -> NEW / RESOLVED
  5. append one line to drift.log; prune snapshots older than --keep-days

Philosophy unchanged from reconcile: observe, never act. No auto-fix, no notify.
Wiring NEW-drift lines into the existing closecraw/Telegram path is a deliberate
LATER step, not this one.

Runs where reconcile runs, as a user with key-based ssh to every host
(BatchMode is already set in reconcile). reconcile.py is NOT modified; this is a
sidecar, like reconcile_config.py / ask.py. stdlib only.

  ./watch.py                 # one scheduled run (what cron/timer calls); prints status line
  ./watch.py --verbose       # also print the significant findings
  ./watch.py --fail-on-drift # exit 1 when significant drift is present (for external alerting)

--- install: systemd timer (preferred over cron — journald + clean env) -------
  # /etc/systemd/system/mnemosyne-drift.service
  [Unit]
  Description=mnemosyne drift snapshot
  [Service]
  Type=oneshot
  User=<operator-with-ssh-keys-to-all-hosts>
  WorkingDirectory=/path/to/mnemosyne
  ExecStart=/path/to/mnemosyne/watch.py

  # /etc/systemd/system/mnemosyne-drift.timer
  [Unit]
  Description=run mnemosyne drift snapshot periodically
  [Timer]
  OnCalendar=*:0/30        # every 30 min; OnCalendar=hourly is fine too
  Persistent=true          # catch up a run missed during downtime
  [Install]
  WantedBy=timers.target

  systemctl enable --now mnemosyne-drift.timer
  systemctl list-timers mnemosyne-drift.timer
  journalctl -u mnemosyne-drift.service -n 20

--- or cron (must have a working non-interactive ssh env: passphraseless key, --
    or an agent reachable from cron) -----------------------------------------
  */30 * * * * /path/to/mnemosyne/watch.py >> /path/to/mnemosyne/history/run.err 2>&1

Note: reconcile probes are lightweight (ss + systemctl) and do NOT touch the GPU,
so this is safe to run during a live stream (unlike `ask.py --backend ollama`).
"""

import argparse
import datetime
import glob
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Same rule ask.py uses to decide a MISSING service is "止めてあるだけ" not a fault.
EXPECTED_OFF_LIFECYCLES = {"on-demand", "stream-only"}


def lifecycle_map(inventory):
    """{(hostname, port): lifecycle} for services with a numeric port."""
    return {(h["hostname"], s["port"]): s.get("lifecycle", "?")
            for h in inventory["hosts"] for s in h.get("services", [])
            if isinstance(s.get("port"), int)}


def significant_findings(results, lc):
    """The exact surface ask.py treats as real drift, as a sorted list of stable
    signature strings: INVESTIGATE missing, undoc, failed units, unreachable.
    EXPECTED-OFF missing (on-demand/stream-only) is filtered out here."""
    sig = []
    for r in results:
        host = r["host"]
        for m in r.get("missing", []):
            l = lc.get((host, m["port"]), "?")
            if l in EXPECTED_OFF_LIFECYCLES:
                continue
            sig.append(f"{host} MISSING {'/'.join(m['services'])}:{m['port']}({l})")
        for u in r.get("undoc", []):
            sig.append(f"{host} UNDOC {u['port']}/{u.get('proto', '?')}->{u.get('proc', '?')}")
        for un in r.get("units", []):
            if un.get("active") == "failed":
                sig.append(f"{host} UNIT-FAILED {un['unit']}")
        if r.get("reachable") is False:
            # error text (timeout vs refused) is left out of the signature so a
            # flapping connection doesn't churn NEW/RESOLVED; it's still in the snapshot.
            sig.append(f"{host} UNREACHABLE")
    return sorted(sig)


def run_reconcile(inventory_path, snap_path):
    """Run reconcile.py, writing structured findings to snap_path. Returns the
    parsed results list, or raises on a real engine failure."""
    cmd = [sys.executable, os.path.join(HERE, "reconcile.py"),
           "--inventory", inventory_path, "--json", snap_path, "--no-color"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(snap_path):
        raise RuntimeError((r.stderr or r.stdout or "reconcile.py failed").strip())
    with open(snap_path, encoding="utf-8") as f:
        return json.load(f)


def load_sig(path, lc):
    try:
        with open(path, encoding="utf-8") as f:
            return set(significant_findings(json.load(f), lc))
    except (OSError, json.JSONDecodeError):
        return set()


def prune(history_dir, keep_days):
    if keep_days <= 0:
        return 0
    cutoff = datetime.datetime.now().timestamp() - keep_days * 86400
    removed = 0
    for p in glob.glob(os.path.join(history_dir, "drift-*.json")):
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
                removed += 1
        except OSError:
            pass
    return removed


def main():
    ap = argparse.ArgumentParser(description="scheduled reconcile snapshot + drift timeline")
    ap.add_argument("--inventory", default=os.path.join(HERE, "inventory.json"))
    ap.add_argument("--drift", default=os.path.join(HERE, "drift.json"),
                    help="canonical latest snapshot ask.py reads (kept fresh each run)")
    ap.add_argument("--history", default=os.path.join(HERE, "history"),
                    help="dir for timestamped snapshots + drift.log")
    ap.add_argument("--keep-days", type=int, default=30, help="prune snapshots older than this (0=keep all)")
    ap.add_argument("--verbose", action="store_true", help="also print the significant findings")
    ap.add_argument("--fail-on-drift", action="store_true", help="exit 1 if significant drift present")
    args = ap.parse_args()

    os.makedirs(args.history, exist_ok=True)
    log_path = os.path.join(args.history, "drift.log")
    now = datetime.datetime.now()
    snap_path = os.path.join(args.history, f"drift-{now:%Y%m%d-%H%M%S}.json")

    with open(args.inventory, encoding="utf-8") as f:
        inventory = json.load(f)
    lc = lifecycle_map(inventory)

    # previous = newest existing snapshot, captured BEFORE we write the new one
    existing = sorted(glob.glob(os.path.join(args.history, "drift-*.json")))
    prev_sig = load_sig(existing[-1], lc) if existing else set()

    try:
        results = run_reconcile(args.inventory, snap_path)
    except RuntimeError as e:
        line = f"{now:%Y-%m-%d %H:%M:%S} ERROR reconcile failed: {e}"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, file=sys.stderr)
        sys.exit(2)

    shutil.copyfile(snap_path, args.drift)  # refresh the canonical latest

    sig = set(significant_findings(results, lc))
    new = sorted(sig - prev_sig)
    resolved = sorted(prev_sig - sig)

    status = "CLEAN" if not sig else f"DRIFT({len(sig)})"
    parts = [f"{now:%Y-%m-%d %H:%M:%S}", status]
    if sig:
        parts.append("; ".join(sorted(sig)))
    if new:
        parts.append("+NEW[" + "; ".join(new) + "]")
    if resolved:
        parts.append("-RESOLVED[" + "; ".join(resolved) + "]")
    line = " ".join(parts)

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")

    pruned = prune(args.history, args.keep_days)

    print(line)
    if args.verbose and sig:
        print("\n".join("  " + s for s in sorted(sig)))
    if pruned:
        print(f"# pruned {pruned} snapshot(s) older than {args.keep_days}d", file=sys.stderr)

    if args.fail_on_drift and sig:
        sys.exit(1)


if __name__ == "__main__":
    main()
