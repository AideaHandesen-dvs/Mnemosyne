#!/usr/bin/env python3
"""
reconcile.py - diff the world model (inventory.json) against live host state.

This automates what we did by hand: ssh to each host, collect listening
sockets (ss / netstat) and systemd unit states, then compare against what
inventory.json *claims*. It surfaces three kinds of drift:

  [MISSING]  inventory documents a port, nothing is listening on it
  [UNDOC]    something is listening, inventory does not document it
  [UNIT]     a systemd unit named in inventory is failed/inactive/disabled

Philosophy (learned the hard way): observation alone lies, intent alone lies.
ss told us "process A owns 5004"; the unit file told us "service B wants 5004".
Only the two together revealed the real story (a manual-toggle port share).
So this tool reports raw observed state next to documented intent and lets a
human (or the advisor LLM) reconcile - it does not silently "fix" anything.

OUT OF SCOPE for v0: cross-host flow verification (e.g. "is host A actually
sending to host B:5004"). You can't see a UDP *sender* from a listen probe.
That stays manual / future work.

Requires: python3 on the orchestrator, key-based ssh to each host.
Run it from a host (e.g. your monitoring/orchestration box) where you already have ssh access.

  ./reconcile.py                      # probe all online hosts
  ./reconcile.py --host nas           # one host
  ./reconcile.py --json report.json   # also dump structured findings
  ./reconcile.py --dry-run            # print remote commands, don't connect
"""

import argparse
import json
import re
import shutil
import socket
import subprocess
import sys

# ---- site config (optional; drop a reconcile_config.py next to this file for
# your deployment; the generic engine runs without it on neutral defaults) ----
try:
    import reconcile_config as _cfg
except ImportError:
    _cfg = None


def _cfg_get(name, default):
    return getattr(_cfg, name, default) if _cfg else default


# Per-host ssh target overrides. Default target is the bare hostname (relies on
# ~/.ssh/config / matching usernames). Site overrides (e.g. root@ for OpenWrt
# boxes) live in reconcile_config.SSH_TARGETS.
SSH_TARGETS = _cfg_get("SSH_TARGETS", {})
# Host whose /etc/hosts is the DHCP/naming authority (used by --hosts).
# None => --hosts requires --hosts-file.
HOSTS_SOURCE = _cfg_get("HOSTS_SOURCE", None)
# Primary LAN prefix; subnets outside it are highlighted in --hosts.
# None => no subnet is treated as "primary" (nothing highlighted).
PRIMARY_SUBNET_PREFIX = _cfg_get("PRIMARY_SUBNET_PREFIX", None)

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
            "-o", "StrictHostKeyChecking=accept-new"]

UNIT_RE = re.compile(r'users:\(\("([^"]+)",pid=(\d+)')
# unit name charset, to keep remote shell injection-safe
SAFE_UNIT = re.compile(r'^[A-Za-z0-9@._-]+$')

# is-enabled values that mean "this really is a systemd unit"
REAL_UNIT_ENABLED = {
    "enabled", "enabled-runtime", "disabled", "static", "indirect",
    "generated", "transient", "masked", "alias",
}

# Noise filtering for the [UNDOC] bucket (suppressed unless --all).
EPHEMERAL_MIN = 32768  # Linux 32768-60999 + Windows 49152-65535 dynamic ranges
NOISE_PORTS = {        # generic OS plumbing / discovery, not meaningful services
    25, 67, 68, 123, 135, 137, 138, 139, 323, 500, 546,
    631, 848, 1900, 3702, 4500, 5040, 5050, 5353, 5355, 5357,
} | set(_cfg_get("EXTRA_NOISE_PORTS", set()))  # site-specific additions, if any

# Statuses that mean "intentionally not running" -> skip in bulk runs (an
# explicit --host still probes them, e.g. when you power the device on).
NOT_RUNNING = {"offline", "dormant"}


def color(s, c, on):
    codes = {"red": 31, "yellow": 33, "green": 32, "cyan": 36, "grey": 90, "bold": 1}
    return f"\033[{codes[c]}m{s}\033[0m" if on else s


def os_class(host):
    oss = (host.get("os") or "").lower()
    if "openwrt" in oss or "friendlywrt" in oss:
        return "openwrt"
    if "windows" in oss:
        return "windows"
    return "linux"


def ssh_target(host):
    name = host["hostname"]
    return SSH_TARGETS.get(name, name)


def documented_ports(host):
    """Return {port:int -> [service names]} for services with a numeric port."""
    out = {}
    for svc in host.get("services", []):
        p = svc.get("port")
        if isinstance(p, int):
            out.setdefault(p, []).append(svc.get("name", "?"))
    return out


def candidate_units(host):
    """Unit names worth checking: daemons with ports + system_services that are
    actually system-level systemd units (NOT user units, kernel modules, etc.)."""
    names = set()
    for svc in host.get("services", []):
        n = svc.get("name", "")
        if SAFE_UNIT.match(n):
            names.add(n)
    for svc in host.get("system_services", []):
        if (svc.get("type") == "systemd") and SAFE_UNIT.match(svc.get("name", "")):
            names.add(svc["name"])
    return sorted(names)


# ---------- remote probe builders ----------

def build_remote_cmd(oscls, units):
    if oscls == "windows":
        # netstat avoids powershell quoting hell; -ano gives state + pid
        return "netstat -ano"
    unit_loop = ""
    if oscls == "linux" and units:
        joined = " ".join(units)
        unit_loop = (
            'echo "@@UNITS@@"; for u in ' + joined + '; do '
            'a=$(systemctl is-active "$u" 2>/dev/null); '
            'b=$(systemctl is-enabled "$u" 2>/dev/null); '
            'printf "%s %s %s\\n" "$u" "${a:-n/a}" "${b:-n/a}"; done; '
        )
    listen = ("(sudo -n ss -tulnp 2>/dev/null || ss -tulnp 2>/dev/null || "
              "ss -tuln 2>/dev/null || netstat -tulnp 2>/dev/null)")
    return (
        'echo "@@OS@@"; uname -sr 2>/dev/null; '
        'echo "@@LISTEN@@"; ' + listen + '; ' +
        unit_loop +
        'echo "@@END@@"'
    )


def run_ssh(target, remote_cmd, dry_run):
    cmd = ["ssh"] + SSH_OPTS + [target, remote_cmd]
    if dry_run:
        return None, " ".join(cmd)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        return None, "TIMEOUT"
    except FileNotFoundError:
        sys.exit("error: ssh client not found on this machine")
    if r.returncode != 0 and not r.stdout:
        return None, (r.stderr.strip() or f"ssh exit {r.returncode}")
    return r.stdout, None


def run_local(remote_cmd, dry_run):
    """Run the probe on the orchestrator's own host (no ssh to self)."""
    if dry_run:
        return None, "LOCAL sh -c " + remote_cmd
    try:
        r = subprocess.run(["sh", "-c", remote_cmd],
                           capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        return None, "TIMEOUT"
    if not r.stdout:
        return None, (r.stderr.strip() or f"local exit {r.returncode}")
    return r.stdout, None


# ---------- parsers ----------

def parse_port(local_field):
    """Extract integer port from a Local Address:Port token."""
    # strip interface suffix like 127.0.0.53%lo, keep last :port
    if ":" not in local_field:
        return None, None
    addr, _, port = local_field.rpartition(":")
    addr = addr.strip("[]")
    try:
        return int(port), addr
    except ValueError:
        return None, None


def parse_unix_listen(text):
    """Parse ss OR netstat output -> {port: {'proto','proc','scope'}}.

    Handles three real formats seen in this fleet:
      - ss -tulnp      : 'tcp LISTEN 0 0 0.0.0.0:80 ... users:(("nginx"...'
      - ss -ulnp       : 'UNCONN 0 0 0.0.0.0:5004 ...'        (State-led, no Netid)
      - busybox netstat: 'tcp 0 0 0.0.0.0:80 0.0.0.0:* LISTEN' (TCP has State at END)
                         'udp 0 0 0.0.0.0:53 0.0.0.0:*'        (UDP has NO State col)
    Strategy: identify proto from the first proto-like token. A row counts as a
    listening/bound socket if it has LISTEN/UNCONN anywhere OR it is a udp row
    (busybox udp servers have no state word). The local addr is the first
    addr:port token with a numeric port; the peer token uses '*' and is skipped.
    """
    found = {}
    for line in text.splitlines():
        toks = line.split()
        if not toks:
            continue
        # skip headers
        if toks[0] in ("Active", "Proto", "Netid", "State"):
            continue
        proto_raw = next((t.lower() for t in toks[:2]
                          if t.lower() in ("tcp", "udp", "tcp6", "udp6")), "")
        is_udp = proto_raw.startswith("udp")
        has_listen = any(t.upper() == "LISTEN" for t in toks)
        has_unconn = any(t.upper() == "UNCONN" for t in toks)
        # UNCONN always means a bound (udp) socket, even without a proto token
        if has_unconn:
            is_udp = True
        # accept: explicit LISTEN/UNCONN, OR a udp row (busybox udp has no state)
        if not (has_listen or has_unconn or is_udp):
            continue
        # a tcp row carrying a non-LISTEN state (ESTAB etc.) is not a server
        if not is_udp and not has_listen and any(
                t.upper() in ("ESTAB", "TIME-WAIT", "CLOSE-WAIT", "SYN-SENT") for t in toks):
            continue
        local = None
        for t in toks:
            # Local addr is the first token with a NUMERIC port. Don't blanket-skip
            # tokens containing '*': the peer column (*:*  0.0.0.0:*  [::]:*) is
            # already rejected because parse_port() fails on a '*' port, while a
            # dual-stack listen addr shown by ss as '*:9100' is a real local addr.
            if ":" in t:
                port, addr = parse_port(t)
                if port is not None:
                    local = (port, addr)
                    break
        if local is None:
            continue
        port, addr = local
        proto = ("udp" if is_udp else "tcp") if proto_raw else ("udp" if is_udp else "tcp")
        m = UNIT_RE.search(line)
        proc = m.group(1) if m else ""
        scope = "localhost" if addr in ("127.0.0.1", "::1", "localhost") else "any"
        cur = found.get(port)
        if not cur or (not cur["proc"] and proc):
            found[port] = {"proto": proto, "proc": proc, "scope": scope}
    return found


def parse_windows_netstat(text):
    found = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        proto = parts[0].lower()
        if proto not in ("tcp", "udp"):
            continue
        local = parts[1]
        port, addr = parse_port(local)
        if port is None:
            continue
        if proto == "tcp" and (len(parts) < 4 or parts[3].upper() != "LISTENING"):
            continue
        scope = "localhost" if addr in ("127.0.0.1", "::1") else "any"
        found.setdefault(port, {"proto": proto, "proc": "", "scope": scope})
    return found


def parse_units(text):
    states = {}
    for line in text.splitlines():
        p = line.split()
        if len(p) == 3:
            name, active, enabled = p
            states[name] = (active, enabled)
    return states


# ---------- host-level drift (inventory vs the naming authority's /etc/hosts) ----------

MOBILE_HINT = re.compile(r'(pixel|iphone|ipad|android|phone|watch)', re.I)


def parse_etc_hosts(text):
    """Return [(ip, [names...])] from /etc/hosts, skipping loopback/ipv6 mcast."""
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        ip, names = parts[0], parts[1:]
        if not names:
            continue
        if ip.startswith(("127.", "::1", "ff02", "fe80")):
            continue
        out.append((ip, names))
    return out


def host_drift(inv_hosts, hosts_text, use_color):
    c = lambda s, col: color(s, col, use_color)
    canon = {}
    for ip, names in parse_etc_hosts(hosts_text):
        canon[names[0]] = ip
    inv_ip = {h["hostname"]: h.get("ip") for h in inv_hosts}
    inv_names = set(inv_ip)
    canon_names = set(canon)

    print(c("\nhost-level drift (inventory vs naming authority /etc/hosts)", "bold"))

    new = sorted(canon_names - inv_names)
    for n in new:
        hint = "  (mobile client?)" if MOBILE_HINT.search(n) else ""
        print("  " + c(f"[NEW]     {n} ({canon[n]}) in DHCP, not in inventory{hint}", "cyan"))

    only_inv = sorted(inv_names - canon_names)
    for n in only_inv:
        ip = inv_ip[n] or "?"
        print("  " + c(f"[ABSENT]  {n} ({ip}) in inventory, not in DHCP "
                       f"(static/offline?)", "grey"))

    for n in sorted(inv_names & canon_names):
        if inv_ip[n] and inv_ip[n] not in ("unknown", None) and inv_ip[n] != canon[n]:
            print("  " + c(f"[IP]      {n}: inventory {inv_ip[n]} != DHCP {canon[n]}", "yellow"))

    subnets = {}
    for ip in canon.values():
        net = ".".join(ip.split(".")[:3]) + ".0/24"
        subnets[net] = subnets.get(net, 0) + 1
    print(c("  subnets seen: ", "bold"), end="")
    parts = []
    for net, cnt in sorted(subnets.items()):
        s = f"{net}({cnt})"
        # highlight non-primary subnets; if no primary configured, highlight none
        if PRIMARY_SUBNET_PREFIX and not net.startswith(PRIMARY_SUBNET_PREFIX):
            s = c(s, "yellow")
        parts.append(s)
    print(", ".join(parts))
    print("\n" + c("summary: ", "bold") +
          f"{len(new)} new, {len(only_inv)} absent, {len(subnets)} subnet(s)")


def split_sections(stdout):
    secs = {"OS": "", "LISTEN": "", "UNITS": ""}
    cur = None
    for line in stdout.splitlines():
        if line.startswith("@@OS@@"):
            cur = "OS"; continue
        if line.startswith("@@LISTEN@@"):
            cur = "LISTEN"; continue
        if line.startswith("@@UNITS@@"):
            cur = "UNITS"; continue
        if line.startswith("@@END@@"):
            cur = None; continue
        if cur:
            secs[cur] += line + "\n"
    return secs


# ---------- reconcile one host ----------

def reconcile_host(host, dry_run, show_all=False, local_name=None):
    oscls = os_class(host)
    target = ssh_target(host)
    units = candidate_units(host) if oscls == "linux" else []
    remote = build_remote_cmd(oscls, units)

    is_local = local_name and host["hostname"].lower() == local_name.lower()
    if is_local:
        stdout, err = run_local(remote, dry_run)
        target = "localhost"
    else:
        stdout, err = run_ssh(target, remote, dry_run)

    result = {"host": host["hostname"], "os": oscls, "target": target,
              "reachable": None, "missing": [], "undoc": [], "units": [],
              "hidden": 0, "error": None, "dry_cmd": None}

    if dry_run:
        result["dry_cmd"] = err
        return result
    if stdout is None:
        result["reachable"] = False
        result["error"] = err
        return result
    result["reachable"] = True

    if oscls == "windows":
        listening = parse_windows_netstat(stdout)
        unit_states = {}
    else:
        secs = split_sections(stdout)
        listening = parse_unix_listen(secs["LISTEN"])
        unit_states = parse_units(secs["UNITS"])

    doc = documented_ports(host)
    doc_ports = set(doc.keys())
    live_ports = set(listening.keys())

    # [MISSING] documented but not listening (never filtered - high value)
    for p in sorted(doc_ports - live_ports):
        result["missing"].append({"port": p, "services": doc[p]})

    # [UNDOC] listening but not documented; suppress noise unless show_all
    for p in sorted(live_ports - doc_ports):
        info = listening[p]
        noise = (p >= EPHEMERAL_MIN or p in NOISE_PORTS
                 or info["scope"] == "localhost")
        if noise and not show_all:
            result["hidden"] += 1
            continue
        result["undoc"].append({"port": p, "proc": info["proc"] or "?",
                                 "proto": info["proto"], "scope": info["scope"]})

    # [UNIT] state of named units
    for name, (active, enabled) in sorted(unit_states.items()):
        if enabled not in REAL_UNIT_ENABLED and active in ("n/a", "unknown", "inactive"):
            continue  # not actually a systemd unit (docker/manual/typo)
        result["units"].append({"unit": name, "active": active, "enabled": enabled})
    return result


# ---------- reporting ----------

def print_report(results, use_color):
    c = lambda s, col: color(s, col, use_color)
    total = {"missing": 0, "undoc": 0, "failed": 0, "unreach": 0, "hidden": 0}

    for r in results:
        head = f"{r['host']}  ({r['os']}, via {r['target']})"
        print("\n" + c(head, "bold"))
        if r.get("dry_cmd"):
            print("  " + c(r["dry_cmd"], "grey")); continue
        if not r["reachable"]:
            total["unreach"] += 1
            print("  " + c(f"[UNREACHABLE] {r['error']}", "red")); continue

        total["hidden"] += r.get("hidden", 0)
        if not (r["missing"] or r["undoc"] or any(
                u["active"] == "failed" for u in r["units"])):
            print("  " + c("clean - inventory matches live state", "green"))

        for m in r["missing"]:
            total["missing"] += 1
            print("  " + c(f"[MISSING] :{m['port']} documented "
                           f"({', '.join(m['services'])}) but nothing listening", "yellow"))
        for u in r["undoc"]:
            total["undoc"] += 1
            loc = "  (localhost)" if u["scope"] == "localhost" else ""
            print("  " + c(f"[UNDOC]   :{u['port']}/{u['proto']} listening "
                           f"-> {u['proc']}{loc}, not in inventory", "cyan"))
        for u in r["units"]:
            if u["active"] == "failed":
                total["failed"] += 1
                print("  " + c(f"[UNIT]    {u['unit']}: FAILED "
                               f"(enabled={u['enabled']})", "red"))
            else:
                print("  " + c(f"[unit]    {u['unit']}: {u['active']} / {u['enabled']}", "grey"))

    extra = ""
    if total["hidden"]:
        extra = f"  ({total['hidden']} noise/ephemeral/localhost hidden; --all to show)"
    print("\n" + c("summary: ", "bold") +
          f"{total['missing']} missing, {total['undoc']} undocumented, "
          f"{total['failed']} failed units, {total['unreach']} unreachable" + extra)


def main():
    ap = argparse.ArgumentParser(description="diff inventory.json vs live hosts")
    ap.add_argument("--inventory", default="inventory.json")
    ap.add_argument("--host", help="only probe this hostname")
    ap.add_argument("--json", metavar="PATH", help="dump findings as JSON")
    ap.add_argument("--dry-run", action="store_true", help="print ssh commands only")
    ap.add_argument("--all", action="store_true",
                    help="show ephemeral/localhost/OS-noise ports too")
    ap.add_argument("--hosts", action="store_true",
                    help="host-level drift: diff inventory against the naming authority's /etc/hosts")
    ap.add_argument("--hosts-file", metavar="PATH",
                    help="read /etc/hosts from a local copy instead of fetching from the naming authority")
    ap.add_argument("--include-clients", action="store_true",
                    help="also probe class:client/iot hosts (phones, ESP32, etc.)")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    use_color = (not args.no_color) and sys.stdout.isatty()
    local_name = socket.gethostname().split(".")[0]

    with open(args.inventory) as f:
        data = json.load(f)
    hosts = data["hosts"] if isinstance(data, dict) else data

    if args.hosts:
        if args.hosts_file:
            with open(args.hosts_file) as f:
                txt = f.read()
        elif HOSTS_SOURCE:
            src = SSH_TARGETS.get(HOSTS_SOURCE, HOSTS_SOURCE)
            txt, err = run_ssh(src, "cat /etc/hosts", args.dry_run)
            if txt is None:
                sys.exit(f"could not fetch {HOSTS_SOURCE} /etc/hosts: {err}\n"
                         f"(fix the {HOSTS_SOURCE} host key, or pass --hosts-file)")
        else:
            sys.exit("--hosts needs a naming authority: set HOSTS_SOURCE in "
                     "reconcile_config.py, or pass --hosts-file")
        host_drift(hosts, txt, use_color)
        return

    targets = []
    for h in hosts:
        if args.host and h["hostname"] != args.host:
            continue
        if h.get("status") in NOT_RUNNING and not args.host:
            continue  # intentionally off (offline/dormant); --host overrides
        if h.get("class") in ("client", "iot") and not args.include_clients:
            continue  # phones / ESP32 etc. are not SSH-able infra
        if (h.get("ip") in (None, "unknown")) and h["hostname"] not in SSH_TARGETS:
            # no ip and not an ssh-config alias we know -> skip
            continue
        targets.append(h)

    if not targets:
        sys.exit("no probeable hosts matched")

    if not shutil.which("ssh") and not args.dry_run:
        sys.exit("error: ssh not found")

    results = [reconcile_host(h, args.dry_run, show_all=args.all,
                              local_name=local_name) for h in targets]
    print_report(results, use_color)

    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
