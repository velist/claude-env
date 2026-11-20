#!/usr/bin/env python3
"""claude-env: audit, retest and rescue a Clash-based workstation.

Read-only unless a subcommand explicitly mutates something
(`guardian install`, `guardian uninstall`, `rescue`).
"""

import argparse
import concurrent.futures
import json
import os
import re
import struct
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

STATE = Path(os.environ.get("CLAUDE_ENV_STATE", Path.home() / ".local/state/claude-env"))
CONFIG = Path(os.environ.get("CLAUDE_ENV_CONFIG", Path.home() / ".config/claude-env/config.json"))
LAUNCH_LABEL = "local.claude-env.guardian"
PLIST = Path.home() / f"Library/LaunchAgents/{LAUNCH_LABEL}.plist"
GUARDIAN_STATUS = Path.home() / "Library/Application Support/ClaudeEnv/status.json"
GUARDIAN_LOG = Path.home() / "Library/Logs/claude-env-guardian.log"

SYMBOL = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌", "SKIP": "⏭ ", "INFO": "ℹ️ "}
RANK = {"PASS": 0, "SKIP": 0, "INFO": 0, "WARN": 1, "FAIL": 2}


def run(args, timeout=10):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return subprocess.CompletedProcess(args, 124, "", "")


def curl(args, timeout=12):
    base = ["/usr/bin/curl", "--noproxy", "*", "-sS", "--max-time", str(timeout)]
    return run(base + args, timeout=timeout + 3)


CONFIG_DIRS = {
    "Clash Verge Rev": Path.home() / "Library/Application Support/io.github.clash-verge-rev.clash-verge-rev",
    "Mihomo Party": Path.home() / "Library/Application Support/mihomo-party",
    "ClashX Pro": Path.home() / "Library/Application Support/ClashX Pro",
    "ClashX Meta": Path.home() / "Library/Application Support/ClashX Meta",
}


def user_config():
    if CONFIG.exists():
        try:
            return json.loads(CONFIG.read_text())
        except ValueError:
            return {}
    return {}


def controller_secrets():
    """Collect (host, port, secret) candidates from known client configs."""
    found = []
    for name, directory in CONFIG_DIRS.items():
        for file in directory.glob("*.y*ml"):
            try:
                text = file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            secret = re.search(r"^\s*secret:\s*['\"]?([^'\"\s]+)", text, re.M)
            controller = re.search(r"^\s*external-controller:\s*['\"]?([0-9.:]+)", text, re.M)
            if not controller:
                continue
            host, _, port = controller.group(1).rpartition(":")
            found.append({"kind": "tcp", "host": host or "127.0.0.1",
                          "port": int(port or 9090), "secret": secret.group(1) if secret else "",
                          "client": name})
    return found


def socket_paths():
    found = sorted(Path("/var/run/clash-verge-service/users").glob("*/verge-mihomo.sock"))
    for extra in ("/var/run/mihomo.sock", "/tmp/mihomo.sock",
                  Path.home() / ".config/mihomo/mihomo.sock"):
        if Path(extra).exists():
            found.append(Path(extra))
    return found


def controller_candidates():
    candidates = []
    override_socket = os.environ.get("MIHOMO_SOCKET")
    if override_socket and Path(override_socket).exists():
        candidates.append({"kind": "unix", "path": override_socket, "client": "env"})
    override_controller = os.environ.get("MIHOMO_CONTROLLER")
    if override_controller:
        host, _, port = override_controller.rpartition(":")
        candidates.append({"kind": "tcp", "host": host or "127.0.0.1",
                           "port": int(port or 9090),
                           "secret": os.environ.get("MIHOMO_SECRET", ""), "client": "env"})
    for path in socket_paths():
        candidates.append({"kind": "unix", "path": str(path), "client": "mihomo"})
    candidates.extend(controller_secrets())
    for port in (9090, 9097, 9091):
        candidates.append({"kind": "tcp", "host": "127.0.0.1", "port": port,
                           "secret": "", "client": "tcp-scan"})
    configured = user_config().get("controller")
    if configured:
        candidates.insert(0, configured)
    return candidates


class Mihomo:
    def __init__(self, spec):
        self.spec = spec
        if spec["kind"] == "unix":
            self.label = spec["path"]
            self.base = ["--unix-socket", spec["path"], "http://localhost"]
        else:
            self.label = f"{spec['host']}:{spec['port']}"
            self.base = [f"http://{spec['host']}:{spec['port']}"]

    def call(self, path, method="GET", payload=None, timeout=8):
        base = self.base[:-1]
        target = self.base[-1] + path
        args = base[:]
        if self.spec["kind"] == "tcp" and self.spec.get("secret"):
            args += ["-H", f"Authorization: Bearer {self.spec['secret']}"]
        args += ["-X", method, target]
        if payload is not None:
            args += ["-H", "Content-Type: application/json",
                     "--data-binary", json.dumps(payload)]
        result = curl(args, timeout=timeout)
        if result.returncode:
            return None
        text = result.stdout.strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except ValueError:
            return None

    def configs(self):
        return self.call("/configs")

    def proxies(self):
        value = self.call("/proxies")
        return (value or {}).get("proxies", {})

    def rules(self):
        value = self.call("/rules")
        return (value or {}).get("rules", [])


def discover_mihomo(timeout=8):
    for spec in controller_candidates():
        if spec["kind"] == "unix" and not Path(spec["path"]).exists():
            continue
        client = Mihomo(spec)
        if client.call("/configs", timeout=timeout) is not None:
            return client
    return None


def runtime_config():
    """Parse the generated runtime YAML for fields the API does not expose."""
    configured = user_config().get("runtime_config")
    candidates = [Path(configured)] if configured else []
    for directory in CONFIG_DIRS.values():
        candidates += [directory / "clash-verge.yaml", directory / "config.yaml",
                       directory / "runtime.yaml"]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace"), path
    return "", None


def yaml_section(text, name):
    lines = text.splitlines()
    out, inside = [], False
    for line in lines:
        if re.match(rf"^{name}:", line):
            inside = True
            out.append(line)
            continue
        if inside:
            if line and not line[0].isspace() and not line.startswith("-"):
                break
            out.append(line)
    return "\n".join(out)


def dns_facts():
    text, path = runtime_config()
    section = yaml_section(text, "dns")
    probe = run(["/usr/bin/dig", "+short", "+time=2", "+tries=1", "claude.ai"]).stdout.split()
    facts = {
        "file": str(path) if path else None,
        "enabled": bool(re.search(r"^\s*enable:\s*true", section, re.M)),
        "fake_ip": bool(re.search(r"enhanced-mode:\s*fake-ip", section)) or
                   any(ip.startswith("198.18.") for ip in probe),
        "behavioral": any(ip.startswith("198.18.") for ip in probe),
        "ipv6": bool(re.search(r"^\s*ipv6:\s*true", section, re.M)),
        "nameservers": re.findall(r"^\s*-\s*([0-9a-zA-Z.\-\[\]:]+#?\S*)\s*$", section, re.M)[:6],
    }
    return facts


def active_service():
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
            address = run(["/usr/sbin/ipconfig", "getifaddr", interface]).stdout.strip()
            return {"interface": interface, "service": service, "address": address}
    return None


def system_dns(service):
    if not service:
        return []
    out = run(["/usr/sbin/networksetup", "-getdnsservers", service["service"]]).stdout
    return [line.strip() for line in out.splitlines() if re.fullmatch(r"[0-9a-fA-F.:]+", line.strip())]


def ipv6_addresses():
    out = run(["/sbin/ifconfig"]).stdout
    return [m for m in re.findall(r"inet6\s+([0-9a-fA-F:]+)", out)
            if not m.startswith(("fe80", "::1", "fd", "fc"))]


def stun_mapped(host="stun.l.google.com", port=19302, timeout=5):
    try:
        import socket
        address = socket.gethostbyname(host)
    except OSError:
        return None
    tid = os.urandom(12)
    packet = struct.pack("!HHI", 0x0001, 0, 0x2112A442) + tid
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (address, port))
        data, _ = sock.recvfrom(2048)
    except OSError:
        return None
    finally:
        sock.close()
    offset = 20
    while offset + 4 <= len(data):
        attr, length = struct.unpack("!HH", data[offset:offset + 4])
        offset += 4
        value = data[offset:offset + length]
        offset += length + ((4 - length % 4) % 4)
        if attr in (0x0020, 0x0001) and len(value) >= 8 and value[1] == 0x01:
            raw = value[4:8]
            mask = struct.pack("!I", 0x2112A442)
            return ".".join(str(a ^ b) for a, b in zip(raw, mask))
    return None


def http_probe(url, timeout=10):
    result = curl(["-o", "/dev/null", "-w",
                   "%{http_code} %{time_namelookup} %{time_appconnect} %{time_total} %{size_download}",
                   "-A", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15", url],
                  timeout=timeout)
    parts = result.stdout.split()
    try:
        code, dns_t, tls_t, total, size = parts[0], float(parts[1]), float(parts[2]), float(parts[3]), int(parts[4])
    except (IndexError, ValueError):
        code, dns_t, tls_t, total, size = "000", 0.0, 0.0, float(timeout), 0
    return {"code": code, "dns": dns_t, "tls": tls_t, "total": total,
            "bytes": size, "ok": result.returncode == 0 and code != "000"}


def foreign_egress():
    result = curl(["https://www.cloudflare.com/cdn-cgi/trace"], timeout=10)
    data = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    return {"ip": data.get("ip"), "loc": data.get("loc"), "colo": data.get("colo")}


def cn_egress():
    result = curl(["https://myip.ipip.net"], timeout=8)
    text = result.stdout.strip()
    match = re.search(r"IP：([0-9.]+)\s+来自于：(.+)", text)
    if match:
        return {"ip": match.group(1), "where": match.group(2).strip()}
    return {"ip": re.search(r"([0-9]{1,3}(?:\.[0-9]{1,3}){3})", text).group(1) if re.search(r"([0-9]{1,3}(?:\.[0-9]{1,3}){3})", text) else None,
            "where": None}


def ip_profile(ip):
    if not ip:
        return {}
    result = curl([f"https://ipinfo.io/{ip}/json"], timeout=10)
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return {}
    return {k: data.get(k) for k in ("ip", "city", "region", "country", "org")}


def rule_snapshot(client):
    rules = client.rules() if client else []
    entries = [(r.get("type"), r.get("payload"), r.get("proxy")) for r in rules]
    final = next((proxy for kind, payload, proxy in reversed(entries) if kind == "Match"), None)
    return {"entries": entries, "final": final, "count": len(entries)}


def check_paypal_direct(rules):
    payloads = [p for _, p, proxy in rules["entries"]
                if proxy == "DIRECT" and p and "paypal" in p.lower()]
    return payloads


def check_wechat_direct(rules):
    wanted = ("weixin.qq.com", "wechat.com", "weixin.com", "qpic.cn", "qlogo.cn")
    found = [p for _, p, proxy in rules["entries"] if proxy == "DIRECT" and p in wanted]
    if found:
        return {"mode": "explicit", "rules": found}
    covers_cn = any(kind == "RuleSet" and "cn" in (payload or "").lower()
                    for kind, payload, _ in rules["entries"])
    if covers_cn:
        return {"mode": "ruleset", "rules": []}
    return {"mode": "none", "rules": []}


def collect(quick=False):
    client = discover_mihomo()
    configs = client.configs() if client else None
    network = active_service()
    facts = dns_facts()
    rules = rule_snapshot(client)

    data = {
        "time": time.time(),
        "host": os.uname().nodename,
        "mihomo": client.label if client else None,
        "client": client.spec.get("client") if client else None,
        "network": network,
        "mode": (configs or {}).get("mode"),
        "tun": ((configs or {}).get("tun") or {}).get("enable"),
        "system_dns": system_dns(network),
        "dns": facts,
        "ipv6": ipv6_addresses(),
        "rules": rules,
        "guardian": guardian_state(),
    }
    if quick:
        return data

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            "foreign": pool.submit(foreign_egress),
            "cn": pool.submit(cn_egress),
            "stun": pool.submit(stun_mapped),
            "stun_cn": pool.submit(stun_mapped, "stun.chat.bilibili.com", 3478),
        }
        for key, future in futures.items():
            try:
                data[key] = future.result(timeout=15)
            except Exception:
                data[key] = None
    data["profile"] = ip_profile((data.get("foreign") or {}).get("ip"))
    data["services"] = service_matrix()
    return data


SERVICES = {
    "claude.ai": "https://claude.ai/",
    "api.anthropic.com": "https://api.anthropic.com/",
    "paypal.com": "https://www.paypal.com/",
    "appleid.apple.com": "https://appleid.apple.com/sign-in",
    "weixin.qq.com": "https://weixin.qq.com/",
}


def service_list():
    return user_config().get("services") or SERVICES


def service_matrix():
    services = service_list()
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(http_probe, services.values()))
    return dict(zip(services.keys(), results))


def guardian_state():
    loaded = run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{LAUNCH_LABEL}"]).returncode == 0
    status = None
    if GUARDIAN_STATUS.exists():
        try:
            status = json.loads(GUARDIAN_STATUS.read_text())
        except ValueError:
            status = None
    age = None
    if status and status.get("time"):
        age = round(time.time() - status["time"], 1)
    return {"installed": loaded, "plist": str(PLIST) if PLIST.exists() else None,
            "status": status.get("status") if status else None,
            "failures": (status or {}).get("failures"),
            "age": age}


def evaluate(data):
    checks = []

    def add(key, title, status, detail, hint=""):
        checks.append({"key": key, "title": title, "status": status,
                       "detail": detail, "hint": hint})

    if not data["mihomo"]:
        add("mihomo", "代理内核", "FAIL", "找不到 mihomo 控制接口",
            "启动 Clash Verge Rev 后再测;或设置 MIHOMO_SOCKET")
    elif data["mode"] != "rule":
        add("mode", "运行模式", "FAIL", f"mode={data['mode']}",
            "切到规则模式:全局会让国内站点也显示境外 IP,direct 则等于没代理")
    else:
        add("mode", "运行模式", "PASS", "rule 规则模式")

    if data["mihomo"]:
        add("tun", "TUN 接管", "PASS" if data["tun"] else "WARN",
            "已开启" if data["tun"] else "未开启,只有走系统代理的 App 生效",
            "" if data["tun"] else "开启 TUN;否则终端和部分 App 会绕过代理")

    foreign = data.get("foreign") or {}
    profile = data.get("profile") or {}
    if not foreign.get("ip"):
        add("egress", "境外出口", "FAIL", "无法获取境外出口 IP", "检查节点是否可用")
    elif profile.get("country") == "CN":
        add("egress", "境外出口", "FAIL",
            f"{foreign['ip']} 归属 CN({profile.get('org')})", "境外流量落到了国内出口,检查 MATCH 规则")
    else:
        add("egress", "境外出口", "PASS",
            f"{foreign['ip']} {profile.get('country')} {profile.get('city') or ''} {profile.get('org') or ''}".strip())

    cn = data.get("cn") or {}
    if cn.get("ip") and cn.get("ip") == foreign.get("ip"):
        add("cn_split", "国内分流", "WARN",
            f"国内外出口相同({cn['ip']}),国内站点也走了代理",
            "若微信/网银卡顿,给国内域名加 DIRECT 规则")
    elif cn.get("ip"):
        add("cn_split", "国内分流", "PASS", f"国内直连出口 {cn['ip']} {cn.get('where') or ''}".strip())
    else:
        add("cn_split", "国内分流", "SKIP", "未能取到国内出口")

    if data["ipv6"]:
        add("ipv6", "IPv6 泄露", "FAIL",
            f"存在全局 IPv6 地址 {data['ipv6'][:2]}", "关闭 Wi-Fi 的 IPv6,或在 mihomo 中接管 v6")
    else:
        add("ipv6", "IPv6 泄露", "PASS", "无全局 IPv6 地址")

    stun = data.get("stun")
    if stun and foreign.get("ip"):
        if stun == foreign["ip"]:
            add("stun", "UDP/WebRTC 出口", "PASS", f"STUN 映射 {stun}")
        else:
            add("stun", "UDP/WebRTC 出口", "FAIL",
                f"STUN 映射 {stun} ≠ 代理出口 {foreign['ip']}",
                "UDP 没走代理,浏览器 WebRTC 会暴露真实 IP")
    else:
        add("stun", "UDP/WebRTC 出口", "SKIP", "STUN 探测无响应")

    stun_cn = data.get("stun_cn")
    if stun_cn and foreign.get("ip") and stun_cn != foreign["ip"]:
        add("stun_cn", "国内 STUN 泄露", "FAIL",
            f"国内 STUN 服务器看到的出口是 {stun_cn}",
            "加前置规则 DOMAIN-KEYWORD,stun,代理组;国内 STUN 命中直连规则时会暴露真实 IP")
    elif stun_cn:
        add("stun_cn", "国内 STUN 泄露", "PASS", f"国内 STUN 也走代理({stun_cn})")
    else:
        add("stun_cn", "国内 STUN 泄露", "SKIP", "国内 STUN 探测无响应")

    dns = data["dns"]
    system = ",".join(data["system_dns"]) or "自动"
    if dns["fake_ip"] and dns["behavioral"]:
        add("dns", "DNS 接管", "PASS",
            f"fake-ip 实测生效(解析为 198.18.x),系统 DNS {system}")
    elif dns["fake_ip"]:
        add("dns", "DNS 接管", "PASS", f"配置为 fake-ip,系统 DNS {system}")
    elif dns["file"] is None and not dns["behavioral"]:
        add("dns", "DNS 接管", "SKIP",
            f"未找到运行时配置,系统 DNS {system}",
            "在 config.json 里用 runtime_config 指定 Clash 的运行时 yaml")
    elif dns["enabled"]:
        add("dns", "DNS 接管", "WARN", f"DNS 已启用但不是 fake-ip(系统 DNS {system})",
            "fake-ip 能避免解析结果暴露给本地运营商")
    else:
        add("dns", "DNS 接管", "FAIL", f"DNS 未启用,系统 DNS {system}",
            "开启 DNS 并设置 fake-ip")

    rules = data["rules"]
    if rules["final"] in (None, "DIRECT", "🚀直接连接"):
        add("final", "兜底规则", "FAIL", f"MATCH -> {rules['final']}",
            "兜底指向代理组,否则未命中的境外流量会直连")
    else:
        add("final", "兜底规则", "PASS", f"MATCH -> {rules['final']}")

    paypal = check_paypal_direct(rules)
    add("paypal", "PayPal 直连", "PASS" if paypal else "WARN",
        ",".join(paypal) if paypal else "未发现 paypal 直连规则",
        "" if paypal else "PayPal 走部分节点会 TLS 握手超时,建议加 DOMAIN-SUFFIX 直连")

    wechat = check_wechat_direct(rules)
    if wechat["mode"] == "explicit":
        add("wechat", "微信直连", "PASS", f"{len(wechat['rules'])} 条显式直连规则")
    elif wechat["mode"] == "ruleset":
        add("wechat", "微信直连", "PASS", "由 cn-domain 规则集覆盖(国内域名直连)")
    else:
        add("wechat", "微信直连", "WARN", "未发现微信直连规则,且没有国内域名规则集",
            "微信图片走代理容易发不出去,建议 qpic.cn/qlogo.cn 直连")

    slow = []
    failed = []
    for name, result in data.get("services", {}).items():
        if not result["ok"]:
            failed.append(name)
        elif result["total"] > 5:
            slow.append(f"{name} {result['total']:.1f}s")
    if failed:
        add("services", "关键站点", "FAIL", "不可达: " + ", ".join(failed),
            "逐项排查是节点、规则还是站点风控")
    elif slow:
        add("services", "关键站点", "WARN", "偏慢: " + ", ".join(slow),
            "换节点或把该域名改走直连")
    else:
        add("services", "关键站点", "PASS", "claude / anthropic / paypal / appleid / wechat 均正常")

    guardian = data["guardian"]
    if guardian["installed"] and guardian["age"] is not None and guardian["age"] < 300:
        add("guardian", "断网自救", "PASS",
            f"运行中,上次 {guardian['age']}s 前,状态 {guardian['status']}")
    elif guardian["installed"]:
        add("guardian", "断网自救", "WARN", "已加载但最近没有心跳", "重新 bootstrap 或看日志")
    else:
        add("guardian", "断网自救", "WARN", "未安装", "guardian install 安装;断网时可自动恢复本地上网")

    return checks


def render(data, checks):
    lines = []
    profile = data.get("profile") or {}
    lines.append(f"claude-env 体检 @ {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"主机 {data['host']}  接口 {(data['network'] or {}).get('interface', '?')}"
                 f"  {(data['network'] or {}).get('address', '')}")
    lines.append(f"内核 {data['mihomo'] or '未连接'}  mode={data['mode']}  tun={data['tun']}")
    lines.append(f"境外出口 {profile.get('ip') or '?'} {profile.get('country') or ''}"
                 f" {profile.get('city') or ''} {profile.get('org') or ''}")
    lines.append("")
    for check in checks:
        lines.append(f"{SYMBOL[check['status']]} {check['title']:<12} {check['detail']}")
        if check["hint"] and check["status"] in ("WARN", "FAIL"):
            lines.append(f"   → {check['hint']}")
    bad = sum(1 for c in checks if c["status"] == "FAIL")
    warn = sum(1 for c in checks if c["status"] == "WARN")
    lines.append("")
    lines.append(f"结果:{len(checks) - bad - warn} 通过 / {warn} 警告 / {bad} 失败")
    return "\n".join(lines)


def worst(checks):
    return max((RANK[c["status"]] for c in checks), default=0)


def save(name, payload):
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)
    return path


def load(name):
    path = STATE / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except ValueError:
        return None


def cmd_audit(args):
    data = collect(quick=args.quick)
    checks = evaluate(data)
    snapshot = {"data": data, "checks": {c["key"]: c["status"] for c in checks},
                "metrics": {"ip": (data.get("profile") or {}).get("ip"),
                            "country": (data.get("profile") or {}).get("country"),
                            "org": (data.get("profile") or {}).get("org"),
                            "ipv6": bool(data["ipv6"]), "stun": data.get("stun"),
                            "stun_cn": data.get("stun_cn"),
                            "mode": data["mode"], "tun": data["tun"]}}
    save("last.json", snapshot)
    save(time.strftime("audit-%Y%m%d-%H%M%S.json"), snapshot)
    print(render(data, checks))
    return worst(checks)


def diff(old, new):
    if not old:
        return ["没有历史快照,这次作为基线"]
    changes = []
    for key, status in new["checks"].items():
        before = old["checks"].get(key)
        if before != status:
            changes.append(f"{key}: {before} → {status}")
    for key, value in new["metrics"].items():
        before = old["metrics"].get(key)
        if before != value:
            changes.append(f"{key}: {before} → {value}")
    return changes


def cmd_retest(args):
    old = load("last.json")
    data = collect()
    checks = evaluate(data)
    snapshot = {"data": data, "checks": {c["key"]: c["status"] for c in checks},
                "metrics": {"ip": (data.get("profile") or {}).get("ip"),
                            "country": (data.get("profile") or {}).get("country"),
                            "org": (data.get("profile") or {}).get("org"),
                            "ipv6": bool(data["ipv6"]), "stun": data.get("stun"),
                            "stun_cn": data.get("stun_cn"),
                            "mode": data["mode"], "tun": data["tun"]}}
    changes = diff(old, snapshot)
    save("last.json", snapshot)
    print(render(data, checks))
    print("\n复测对比:")
    for line in changes:
        print("  • " + line)
    return worst(checks)


def cmd_services(args):
    data = collect(quick=True)
    results = service_matrix()
    for name, result in results.items():
        flag = "ok" if result["ok"] and result["total"] <= 5 else "slow" if result["ok"] else "fail"
        print(f"{name:<20} {flag:<5} code={result['code']} tls={result['tls']:.2f}s "
              f"total={result['total']:.2f}s bytes={result['bytes']}")
    return 0


def cmd_doctor(args):
    print(f"配置档 {CONFIG}{'' if CONFIG.exists() else '(不存在,使用默认值)'}")
    print(f"状态目录 {STATE}")
    print()
    print("控制接口探测:")
    working = None
    for spec in controller_candidates():
        if spec["kind"] == "unix" and not Path(spec["path"]).exists():
            continue
        client = Mihomo(spec)
        configs = client.configs()
        mark = "OK " if configs is not None else "ERR"
        where = spec.get("path") or f"{spec['host']}:{spec['port']}"
        print(f"  {mark} [{spec.get('client', '?')}] {where}")
        if configs is not None and working is None:
            working = client
    print()
    if working:
        configs = working.call("/configs") or {}
        print(f"使用 {working.label}")
        print(f"  mode={configs.get('mode')}  mixed-port={configs.get('mixed-port')}  "
              f"tun={(configs.get('tun') or {}).get('enable')}")
    else:
        print("没有可用的控制接口。支持的客户端:Clash Verge Rev、Mihomo Party、ClashX Meta、")
        print("任何开放 external-controller 的 mihomo 内核。可在 config.json 里手动指定:")
        print('  {"controller": {"kind": "tcp", "host": "127.0.0.1", "port": 9090, "secret": ""}}')
    print()
    text, path = runtime_config()
    print(f"运行时配置 {path or '未找到'}")
    if args.write_config and not CONFIG.exists():
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        template = {
            "controller": {"kind": "unix", "path": working.spec.get("path", "")}
            if working and working.spec["kind"] == "unix" else
            {"kind": "tcp", "host": "127.0.0.1", "port": 9090, "secret": ""},
            "runtime_config": str(path) if path else "",
            "group": "🔰国外流量",
            "services": SERVICES,
        }
        CONFIG.write_text(json.dumps(template, ensure_ascii=False, indent=2))
        CONFIG.chmod(0o600)
        print(f"已写入模板 {CONFIG},按实际客户端修改 group / controller")
    return 0 if working else 2


def cmd_nodes(args):
    client = discover_mihomo()
    if not client:
        print("找不到 mihomo 控制接口")
        return 2
    proxies = client.proxies()
    groups = {k: v for k, v in proxies.items()
              if v.get("type") in ("Selector", "URLTest", "Fallback", "LoadBalance")}
    wanted = args.group or user_config().get("group")
    if wanted:
        groups = {k: v for k, v in groups.items() if k == wanted}
        if not groups:
            print(f"没有名为 {wanted} 的代理组,现有:" + ", ".join(
                f"{k}" for k in proxies if proxies[k].get("type") == "Selector"))
            return 1
    providers = (client.call("/providers/proxies") or {}).get("providers", {})
    nodes = {}
    for provider_name, provider in providers.items():
        for proxy in provider.get("proxies") or []:
            if proxy.get("type") in ("Selector", "URLTest", "Fallback", "LoadBalance",
                                     "Direct", "Reject", "Compatible"):
                continue
            history = proxy.get("history") or []
            nodes[proxy["name"]] = {
                "provider": provider_name,
                "delay": history[-1]["delay"] if history else None,
                "alive": proxy.get("alive"),
            }

    if args.probe:
        for provider_name in {n["provider"] for n in nodes.values() if n["provider"] in providers}:
            client.call(f"/providers/proxies/{urllib.parse.quote(provider_name)}/healthcheck",
                        timeout=20)
        providers = (client.call("/providers/proxies") or {}).get("providers", {})
        for provider in providers.values():
            for proxy in provider.get("proxies") or []:
                if proxy.get("name") in nodes:
                    history = proxy.get("history") or []
                    nodes[proxy["name"]]["delay"] = history[-1]["delay"] if history else None
                    nodes[proxy["name"]]["alive"] = proxy.get("alive")

    for name, group in groups.items():
        print(f"[{group['type']}] {name} -> {group.get('now')}")
        members = [m for m in group.get("all", []) if m in nodes]
        members.sort(key=lambda m: nodes[m]["delay"] or 10 ** 6)
        for member in members[: args.limit]:
            info = nodes[member]
            mark = "OK " if info["alive"] and info["delay"] else "ERR"
            print(f"    {mark} {info['delay'] or '':>5}ms  {member}")
        print()
    if not any(m in nodes for g in groups.values() for m in g.get("all", [])):
        print("这些组只有内建策略(直连/DIRECT),没有可测节点")
    if not args.probe:
        print("延迟来自 provider 最近一次健康检查;加 --probe 强制重测")
    return 0


def cmd_guardian(args):
    script = Path(__file__).with_name("guardian.py")
    if not script.exists():
        print(f"缺少 {script}")
        return 2
    if args.action == "install":
        return install_guardian(script, migrate=args.migrate)
    if args.action == "uninstall":
        return uninstall_guardian()
    if args.action == "status":
        state = guardian_state()
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0 if state["installed"] else 1
    command = [sys.executable, str(script)] + (["--check"] if args.check else [])
    result = run(command, timeout=120)
    print(result.stdout)
    if result.returncode:
        print(result.stderr)
    return result.returncode


def install_guardian(script, migrate=False):
    legacy = Path.home() / "Library/LaunchAgents/com.test.netguardian.plist"
    if legacy.exists() and migrate:
        run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/com.test.netguardian"])
        legacy.unlink()
        print("已停用旧的 com.test.netguardian")
    if legacy.exists() and not os.environ.get("CLAUDE_ENV_ALLOW_DUAL_GUARDIAN"):
        print("检测到旧的 com.test.netguardian,两个看门狗会互相干扰。\n"
              "先执行:launchctl bootout gui/$(id -u)/com.test.netguardian && "
              "rm ~/Library/LaunchAgents/com.test.netguardian.plist\n"
              "或设置 CLAUDE_ENV_ALLOW_DUAL_GUARDIAN=1 强行并存。")
        return 1
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCH_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{sys.executable}</string>
    <string>{script}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>{GUARDIAN_LOG}</string>
  <key>StandardErrorPath</key><string>{GUARDIAN_LOG}</string>
</dict>
</plist>
"""
    PLIST.write_text(plist)
    run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCH_LABEL}"])
    result = run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)])
    print("已安装" if result.returncode == 0 else f"安装失败:{result.stderr}")
    return result.returncode


def uninstall_guardian():
    run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCH_LABEL}"])
    if PLIST.exists():
        PLIST.unlink()
    print("已卸载")
    return 0


def main():
    parser = argparse.ArgumentParser(prog="claude_env")
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="只读体检")
    audit.add_argument("--quick", action="store_true")
    audit.set_defaults(func=cmd_audit)

    retest = sub.add_parser("retest", help="复测并与上次对比")
    retest.set_defaults(func=cmd_retest)

    services = sub.add_parser("services", help="关键站点连通性")
    services.set_defaults(func=cmd_services)

    doctor = sub.add_parser("doctor", help="检测客户端与配置路径")
    doctor.add_argument("--write-config", action="store_true", help="写入配置模板")
    doctor.set_defaults(func=cmd_doctor)

    nodes = sub.add_parser("nodes", help="代理组与节点延迟")
    nodes.add_argument("--group", help="只测某个代理组")
    nodes.add_argument("--limit", type=int, default=15, help="每组显示多少个节点")
    nodes.add_argument("--probe", action="store_true", help="强制触发健康检查")
    nodes.set_defaults(func=cmd_nodes)

    guardian = sub.add_parser("guardian", help="断网自救")
    guardian.add_argument("action", choices=["install", "uninstall", "status", "run"])
    guardian.add_argument("--check", action="store_true", help="只打印状态不修复")
    guardian.add_argument("--migrate", action="store_true", help="接管旧的 com.test.netguardian")
    guardian.set_defaults(func=cmd_guardian)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
