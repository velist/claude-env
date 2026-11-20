#!/usr/bin/env python3
"""Watchdog that restores local connectivity when the tunnel breaks.

Design rules, learned the hard way:
  * never act on one bad sample: three consecutive all-failed rounds are required
  * never act twice within ten minutes
  * never act if the network or the proxy config changed under us
  * verify the physical path first, so a real ISP outage is not "fixed" by disabling TUN
  * keep a snapshot before touching DNS or TUN
"""

import argparse
import concurrent.futures
import fcntl
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_env import discover_mihomo  # noqa: E402

ROOT = Path.home() / "Library/Application Support/ClaudeEnv"
STATUS = ROOT / "status.json"
LOG = Path.home() / "Library/Logs/claude-env-guardian.log"

TARGETS = {
    "domestic_baidu": "https://www.baidu.com/",
    "domestic_qq": "https://www.qq.com/",
    "overseas_google": "https://www.google.com/generate_204",
    "overseas_cloudflare": "https://www.cloudflare.com/cdn-cgi/trace",
}

CONFIRM_ROUNDS = 3
COOLDOWN = 600


def run(args, timeout=7):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as error:
        return subprocess.CompletedProcess(args, 124, "", type(error).__name__)


_client = None
_client_at = 0.0


def api(method="GET", payload=None):
    global _client, _client_at
    if _client is None or time.time() - _client_at > 120:
        _client = discover_mihomo(timeout=3)
        _client_at = time.time()
    if _client is None:
        return None
    return _client.call("/configs", method, payload, timeout=4)


def probe(url, interface=None):
    args = ["/usr/bin/curl", "--noproxy", "*", "-sS", "--connect-timeout", "3",
            "--max-time", "5", "-o", "/dev/null", "-w", "%{http_code}"]
    if interface:
        args += ["--interface", interface]
    result = run(args + [url])
    code = int(result.stdout) if result.stdout.isdigit() else 0
    return {"reachable": result.returncode == 0 and 200 <= code < 600,
            "http": code, "curl_exit": result.returncode}


def connectivity():
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(probe, TARGETS.values()))
    return dict(zip(TARGETS, results))


def active_network():
    info = run(["/usr/sbin/scutil", "--nwi"]).stdout
    match = re.search(r"Network interfaces:\s*(\S+)", info)
    if not match or not re.fullmatch(r"(en\d+|bridge\d+)", match.group(1)):
        return None
    interface = match.group(1)
    order = run(["/usr/sbin/networksetup", "-listnetworkserviceorder"]).stdout
    service = None
    for line in order.splitlines():
        entry = re.match(r"^\((\d+|\*)\) (.+)$", line)
        if entry:
            service = entry.group(2) if entry.group(1) != "*" else None
        if service and f"Device: {interface})" in line:
            return {"interface": interface, "service": service,
                    "address": run(["/usr/sbin/ipconfig", "getifaddr", interface]).stdout.strip()}
    return None


def dns_servers(service):
    out = run(["/usr/sbin/networksetup", "-getdnsservers", service]).stdout
    servers = []
    for line in out.splitlines():
        try:
            servers.append(str(ipaddress.ip_address(line.strip())))
        except ValueError:
            pass
    return servers


def observe():
    network = active_network()
    config = api()
    routes = run(["/usr/sbin/netstat", "-rn", "-f", "inet"]).stdout
    tun_route = any("utun" in line and "198.18." in line for line in routes.splitlines())
    proxy = run(["/usr/sbin/scutil", "--proxy"]).stdout
    system_proxy = bool(re.search(r"(HTTPEnable|HTTPSEnable|SOCKSEnable|ProxyAutoConfigEnable)\s*:\s*1", proxy))
    return {"time": time.time(), "network": network, "config": config,
            "tun_route": tun_route, "system_proxy": system_proxy,
            "dns": dns_servers(network["service"]) if network else [],
            "checks": connectivity() if network else {}}


def classify(snapshot):
    if not snapshot["network"]:
        return "offline"
    domestic = any(v["reachable"] for k, v in snapshot["checks"].items() if k.startswith("domestic"))
    overseas = any(v["reachable"] for k, v in snapshot["checks"].items() if k.startswith("overseas"))
    if domestic and overseas:
        return "healthy"
    if domestic:
        return "overseas_unavailable"
    if overseas:
        return "domestic_unavailable"
    return "all_failed"


def decision(snapshot, previous):
    status = classify(snapshot)
    config = snapshot["config"] or {}
    fingerprint = [snapshot["network"], config.get("mode"), (config.get("tun") or {}).get("enable")]
    consecutive = (previous.get("fingerprint") == fingerprint and
                   0 <= snapshot["time"] - previous.get("time", 0) <= 150)
    failures = previous.get("failures", 0) + 1 if consecutive else 1
    if status != "all_failed":
        failures = 0
    action = "none"
    if failures >= CONFIRM_ROUNDS and snapshot["time"] - previous.get("last_repair", 0) >= COOLDOWN:
        if not snapshot["system_proxy"]:
            if (config.get("tun") or {}).get("enable"):
                action = "release_tun_and_reset_dns"
            elif not snapshot["tun_route"]:
                action = "reset_dns"
    return {"time": snapshot["time"], "status": status, "fingerprint": fingerprint,
            "failures": failures, "action": action,
            "last_repair": previous.get("last_repair", 0)}


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.chmod(0o600)
    temporary.replace(path)


def log(message):
    if LOG.exists() and LOG.stat().st_size > 524288:
        with LOG.open("rb") as handle:
            handle.seek(-262144, 2)
            tail = handle.read().decode("utf-8", errors="replace")
        LOG.write_text(tail)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as handle:
        handle.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")


def repair(snapshot, action):
    interface = snapshot["network"]["interface"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        direct = list(pool.map(
            lambda address: probe(f"https://{address}/dns-query", interface),
            ("223.5.5.5", "223.6.6.6")))
    if not any(item["reachable"] for item in direct):
        return "skipped_direct_path_unavailable"
    if active_network() != snapshot["network"]:
        return "skipped_network_changed"
    if any(item["reachable"] for item in connectivity().values()):
        return "skipped_already_recovered"
    current = api()
    if current != snapshot["config"]:
        return "skipped_config_changed"
    atomic_json(ROOT / "last-repair.json", {
        "time": time.time(), "network": snapshot["network"], "dns_before": snapshot["dns"],
        "tun_before": (current or {}).get("tun"), "action": action})
    if action == "release_tun_and_reset_dns":
        tun = dict(current["tun"])
        tun["enable"] = False
        if api("PATCH", {"tun": tun}) is None:
            return "failed_tun_release"
        updated = api()
        if updated is None or (updated.get("tun") or {}).get("enable") is not False:
            return "failed_tun_verification"
    result = run(["/usr/sbin/networksetup", "-setdnsservers",
                  snapshot["network"]["service"], "223.5.5.5", "119.29.29.29"])
    if result.returncode:
        return "failed_dns_update"
    run(["/usr/bin/dscacheutil", "-flushcache"])
    checks = connectivity()
    domestic = any(v["reachable"] for k, v in checks.items() if k.startswith("domestic"))
    return "local_access_restored" if domestic else "repair_attempted_still_unreachable"


def execute(check_only=False):
    snapshot = observe()
    if check_only:
        print(json.dumps({"status": classify(snapshot), "network": snapshot["network"],
                          "checks": snapshot["checks"], "dns": snapshot["dns"],
                          "mode": (snapshot["config"] or {}).get("mode")},
                         ensure_ascii=False, indent=2))
        return 0
    try:
        previous = json.loads(STATUS.read_text())
    except (OSError, ValueError):
        previous = {}
    state = decision(snapshot, previous)
    state["checks"] = snapshot["checks"]
    if state["status"] != previous.get("status"):
        log("STATUS " + state["status"])
    if state["action"] != "none":
        state["last_repair"] = snapshot["time"]
        atomic_json(STATUS, state)
        state["repair_result"] = repair(snapshot, state["action"])
        log("REPAIR " + state["action"] + " " + state["repair_result"])
        if state["repair_result"] in ("local_access_restored", "skipped_already_recovered"):
            state["failures"] = 0
    atomic_json(STATUS, state)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        return execute(True)
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (ROOT / "lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        return execute()


if __name__ == "__main__":
    raise SystemExit(main())
