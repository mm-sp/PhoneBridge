"""Facts about this PC for the phone app's Device, Ports, Apps and Files tabs
(see protocol/PROTOCOL.md section 4c). Plain functions, safe to run off the GTK thread
except `list_apps`' icon rendering, which only touches GdkPixbuf/files.
"""
from __future__ import annotations

import base64
import configparser
import os
import re
import platform
import shutil
import socket
import subprocess
import time
from pathlib import Path

HOME = Path.home()


def _read(path: str | Path, default: str = "") -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def _cpu_times() -> tuple[int, int]:
    parts = _read("/proc/stat").splitlines()[0].split()[1:]
    values = [int(v) for v in parts]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return idle, sum(values)


def _cpu_percent(interval: float = 0.25) -> float:
    i1, t1 = _cpu_times()
    time.sleep(interval)
    i2, t2 = _cpu_times()
    dt = t2 - t1
    return round(100.0 * (1 - (i2 - i1) / dt), 1) if dt else 0.0


def _meminfo() -> dict[str, int]:
    out = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        out[key] = int(rest.split()[0]) * 1024 if rest.split() else 0
    return out


def _os_name() -> str:
    for line in _read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip('"')
    return platform.system()


def _cpu_model() -> str:
    for line in _read("/proc/cpuinfo").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return platform.processor() or platform.machine()


def _battery() -> dict:
    for supply in sorted(Path("/sys/class/power_supply").glob("BAT*")):
        cap = _read(supply / "capacity")
        if cap:
            return {"battery_pct": int(cap), "battery_status": _read(supply / "status", "Unknown")}
    return {}


def _wifi() -> dict:
    try:
        out = subprocess.run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi"],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    for line in out.splitlines():
        parts = line.split(":")
        if parts and parts[0] == "yes" and len(parts) >= 3:
            return {"wifi_ssid": parts[1], "wifi_signal_pct": int(parts[2] or 0)}
    return {}


def pc_info() -> dict:
    mem = _meminfo()
    root = shutil.disk_usage("/")
    home = shutil.disk_usage(HOME)
    uptime = float(_read("/proc/uptime", "0").split()[0] or 0)
    info = {
        "hostname": socket.gethostname(),
        "user": os.environ.get("USER", ""),
        "os": _os_name(),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
        "session": os.environ.get("XDG_SESSION_TYPE", ""),
        "cpu_model": _cpu_model(),
        "cpu_cores": os.cpu_count() or 0,
        "cpu_pct": _cpu_percent(),
        "load_avg": [round(x, 2) for x in os.getloadavg()],
        "ram_total": mem.get("MemTotal", 0),
        "ram_free": mem.get("MemAvailable", 0),
        "swap_total": mem.get("SwapTotal", 0),
        "swap_free": mem.get("SwapFree", 0),
        "disk_total": root.total,
        "disk_free": root.free,
        "home_total": home.total,
        "home_free": home.free,
        "uptime_s": int(uptime),
        "ips": [a["address"] for i in interfaces() if i["kind"] != "virtual"
                for a in i["addresses"] if ":" not in a["address"]],
        # Real network adapters with MAC + addresses (the phone's Device tab shows these).
        "interfaces": [i for i in interfaces() if i["kind"] != "virtual"],
    }
    info.update(_battery())
    info.update(_wifi())
    return info


# --------------------------------------------------------------------------- network


_IFACE_SKIP = ("lo",)


def interfaces() -> list[dict]:
    """Every network interface with its MAC and addresses; Wi-Fi ones flagged."""
    addrs: dict[str, list[dict]] = {}
    try:
        out = subprocess.run(["ip", "-o", "addr", "show"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            addrs.setdefault(parts[1], []).append({"family": parts[2], "address": parts[3].split("/")[0],
                                                   "prefix": parts[3].split("/")[1] if "/" in parts[3] else ""})
    result = []
    for node in sorted(Path("/sys/class/net").iterdir()):
        name = node.name
        if name in _IFACE_SKIP:
            continue
        kind = "wifi" if (node / "wireless").exists() else (
            "virtual" if name.startswith(("docker", "br-", "veth", "virbr", "tun", "tap")) else "ethernet")
        result.append({
            "name": name,
            "kind": kind,
            "mac": _read(node / "address"),
            "state": _read(node / "operstate", "unknown"),
            "addresses": addrs.get(name, []),
        })
    # Real interfaces first, Wi-Fi at the top.
    order = {"wifi": 0, "ethernet": 1, "virtual": 2}
    return sorted(result, key=lambda i: (order[i["kind"]], i["name"]))


_WELL_KNOWN = {21: "ftp", 22: "ssh", 23: "telnet", 25: "smtp", 53: "dns", 80: "http", 110: "pop3", 139: "netbios",
               143: "imap", 443: "https", 445: "smb", 631: "cups (printing)", 3306: "mysql", 5432: "postgresql",
               5900: "vnc", 6379: "redis", 8080: "http-alt", 27017: "mongodb", 28100: "PhoneBridge",
               28101: "PhoneBridge discovery", 28102: "PhoneBridge pairing", 3389: "rdp", 1883: "mqtt"}


def _service_name(port: int, proto: str) -> str:
    if port in _WELL_KNOWN:
        return _WELL_KNOWN[port]
    try:
        return socket.getservbyport(port, proto)
    except OSError:
        return ""


def listening_ports() -> list[dict]:
    """Open TCP/UDP listening sockets (`ss -tulnp`); process names show for your own
    processes only (root-owned ones like sshd show without a name)."""
    try:
        out = subprocess.run(["ss", "-tulnpH"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    seen = set()
    ports = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        proto = parts[0]
        local = parts[4]
        host, _, port_s = local.rpartition(":")
        if not port_s.isdigit():
            continue
        port = int(port_s)
        process = ""
        if "users:((" in line:
            process = line.split('users:(("', 1)[1].split('"', 1)[0]
        host = host.strip("[]").split("%")[0]
        key = (proto, port, process)
        if key in seen:
            continue  # IPv4 + IPv6 listeners of the same service
        seen.add(key)
        ports.append({
            "proto": proto, "port": port, "address": host or "*",
            "service": _service_name(port, proto), "process": process,
            "exposed": host not in ("127.0.0.1", "::1", "127.0.0.53", "127.0.0.54"),
        })
    return sorted(ports, key=lambda p: (p["port"], p["proto"]))


# --------------------------------------------------------------------------- files


def _inside_home_visible(target: Path) -> bool:
    """True if `target` (already resolved) is HOME or below it with no hidden (dot) component.
    Keeps ~/.ssh, ~/.gnupg, ~/.config/phonebridge (keys!), browser profiles etc. out of reach even
    though a symlink or a typed path could name them; the listing already hides dotfiles."""
    if target == HOME:
        return True
    if HOME not in target.parents:
        return False
    return not any(part.startswith(".") for part in target.relative_to(HOME).parts)


def list_dir(rel: str) -> dict:
    """Listing of a folder inside the user's home (paths are relative to ~)."""
    target = (HOME / rel.lstrip("/")).resolve()
    if not _inside_home_visible(target):
        return {"path": "/", "entries": [], "error": "Only visible folders inside your home folder can be browsed"}
    out = {"path": "/" + str(target.relative_to(HOME)) if target != HOME else "/"}
    try:
        children = list(target.iterdir())
    except OSError as exc:
        return {**out, "entries": [], "error": str(exc)}
    entries = []
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            st = child.stat()
            is_dir = child.is_dir()
        except OSError:
            continue
        size = st.st_size
        if is_dir:
            try:
                size = sum(1 for _ in child.iterdir())
            except OSError:
                size = 0
        entries.append({"name": child.name, "is_dir": is_dir, "size": size, "mtime": int(st.st_mtime * 1000)})
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    usage = shutil.disk_usage(target)
    return {**out, "entries": entries, "free": usage.free, "total": usage.total}


def resolve_home_file(rel: str) -> Path | None:
    target = (HOME / rel.lstrip("/")).resolve()
    if _inside_home_visible(target) and target != HOME and target.is_file():
        return target
    return None


# --------------------------------------------------------------------------- apps


_APP_DIRS = [
    Path("/usr/share/applications"),
    Path("/usr/local/share/applications"),
    HOME / ".local/share/applications",
    Path("/var/lib/flatpak/exports/share/applications"),
    HOME / ".local/share/flatpak/exports/share/applications",
    Path("/var/lib/snapd/desktop/applications"),
]
_ICON_DIRS = [HOME / ".local/share/icons", Path("/usr/share/icons/hicolor"), Path("/usr/share/icons"),
              Path("/usr/share/pixmaps"), Path("/var/lib/flatpak/exports/share/icons/hicolor"),
              Path("/var/lib/snapd/desktop/icons")]


def _find_icon(name: str) -> Path | None:
    if not name:
        return None
    p = Path(name)
    if p.is_absolute():
        return p if p.exists() else None
    for size in ("48x48", "64x64", "scalable", "128x128", "256x256", "32x32"):
        for base in (_ICON_DIRS[1], _ICON_DIRS[4], HOME / ".local/share/icons/hicolor"):
            for ext in ("png", "svg"):
                cand = base / size / "apps" / f"{name}.{ext}"
                if cand.exists():
                    return cand
    for base in (Path("/usr/share/pixmaps"), Path("/var/lib/snapd/desktop/icons")):
        for ext in ("png", "svg", "xpm"):
            cand = base / f"{name}.{ext}"
            if cand.exists():
                return cand
    return None


def _icon_png_b64(path: Path | None, size: int = 48) -> str:
    if path is None:
        return ""
    try:
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf

        pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), size, size, True)
        ok, data = pix.save_to_bufferv("png", [], [])
        return base64.b64encode(data).decode() if ok else ""
    except Exception:  # noqa: BLE001 - unreadable/odd icon formats: no icon
        return ""


def list_apps(on_progress=None) -> list[dict]:
    """Launchable desktop applications (same set the GNOME app grid shows)."""
    found: dict[str, Path] = {}
    for d in _APP_DIRS:
        if d.is_dir():
            for f in d.glob("*.desktop"):
                found[f.name] = f  # later dirs (user, flatpak) override system ones
    apps = []
    items = sorted(found.values())
    for i, path in enumerate(items):
        if on_progress:
            on_progress(i + 1, len(items))
        cp = configparser.RawConfigParser(strict=False, interpolation=None)
        try:
            cp.read(path, encoding="utf-8")
            e = cp["Desktop Entry"]
        except (configparser.Error, KeyError, UnicodeDecodeError):
            continue
        if e.get("Type") != "Application" or e.get("NoDisplay", "").lower() == "true" or \
                e.get("Hidden", "").lower() == "true" or not e.get("Exec"):
            continue
        show_in = e.get("OnlyShowIn", "")
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
        if show_in and not any(d in show_in.split(";") for d in desktop.split(":")):
            continue
        apps.append({"id": path.name, "name": e.get("Name", path.stem), "comment": e.get("Comment", ""),
                     "icon": _icon_png_b64(_find_icon(e.get("Icon", "")))})
    return sorted(apps, key=lambda a: a["name"].lower())


def desktop_file_path(app_id: str) -> Path | None:
    for d in reversed(_APP_DIRS):
        p = d / Path(app_id).name
        if p.exists():
            return p
    return None


# --------------------------------------------------------------------------- servers

_SERVER_UNITS = ("ssh", "sshd", "apache2", "httpd", "nginx", "lighttpd", "caddy", "mysql", "mariadb", "postgresql",
                 "mongod", "redis", "redis-server", "memcached", "docker", "containerd", "podman", "vsftpd", "proftpd",
                 "pure-ftpd", "smbd", "nmbd", "nfs-server", "cups", "postfix", "exim4", "dovecot", "bind9", "named",
                 "dnsmasq", "jenkins", "tomcat", "tomcat9", "tomcat10", "php-fpm", "ollama", "elasticsearch",
                 "rabbitmq-server", "grafana-server", "prometheus", "xrdp", "vncserver", "openvpn", "wireguard",
                 "nginx-proxy", "haproxy", "squid", "mosquitto", "influxdb", "couchdb", "minio", "gitea", "plexmediaserver",
                 "jellyfin", "transmission-daemon", "syncthing")


def _unit_base(unit: str) -> str:
    base = unit.removesuffix(".service")
    base = base.split("@", 1)[0]
    return base.removeprefix("snap.").split(".", 1)[0] if base.startswith("snap.") else base


def docker_containers() -> dict:
    try:
        out = subprocess.run(["docker", "ps", "-a", "--format", "{{json .}}"],
                             capture_output=True, text=True, timeout=8)
    except FileNotFoundError:
        return {"available": False, "error": "Docker isn't installed", "containers": []}
    except subprocess.TimeoutExpired:
        return {"available": False, "error": "Docker didn't answer", "containers": []}
    if out.returncode != 0:
        msg = out.stderr.strip().splitlines()[-1] if out.stderr.strip() else "docker failed"
        if "permission denied" in msg.lower():
            msg = "No permission to use Docker (add your user to the docker group)"
        return {"available": False, "error": msg, "containers": []}
    import json

    containers = []
    for line in out.stdout.splitlines():
        try:
            c = json.loads(line)
        except ValueError:
            continue
        labels = dict(kv.split("=", 1) for kv in c.get("Labels", "").split(",") if "=" in kv)
        containers.append({
            "id": c.get("ID", ""), "name": c.get("Names", ""), "image": c.get("Image", ""),
            "state": c.get("State", ""), "status": c.get("Status", ""), "ports": c.get("Ports", ""),
            "project": labels.get("com.docker.compose.project", ""),
        })
    containers.sort(key=lambda c: (c["state"] != "running", c["project"], c["name"]))
    return {"available": True, "containers": containers}


def system_services() -> list[dict]:
    try:
        out = subprocess.run(["systemctl", "list-units", "--type=service", "--all", "--no-legend", "--plain"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    services = {}
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4 or parts[1] == "not-found":
            continue
        unit, _load, active, sub = parts[:4]
        base = _unit_base(unit)
        if base not in _SERVER_UNITS:
            continue
        prev = services.get(base)
        if prev and prev["active"] == "active":
            continue  # keep the running instance (e.g. cups vs snap.cups.cupsd)
        services[base] = {"name": base, "unit": unit, "active": active, "state": sub,
                          "description": parts[4] if len(parts) > 4 else ""}
    return sorted(services.values(), key=lambda s: (s["active"] != "active", s["name"]))


def servers() -> dict:
    by_process: dict[str, list[str]] = {}
    for p in listening_ports():
        if p["process"] and p["process"] != "phonebridge":
            by_process.setdefault(p["process"], []).append(f'{p["port"]}/{p["proto"]}')
    return {
        "docker": docker_containers(),
        "services": system_services(),
        "programs": [{"process": k, "ports": sorted(set(v))} for k, v in sorted(by_process.items())],
    }


def docker_action(container_id: str, action: str) -> tuple[bool, str]:
    # A container id/name never starts with "-" (that would be read as a docker option) and is plain [A-Za-z0-9_.-].
    if action not in ("start", "stop", "restart") or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container_id or ""):
        return False, "Unknown action"
    try:
        out = subprocess.run(["docker", action, container_id], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if out.returncode == 0:
        return True, f"{action.capitalize()}ed {container_id}" if action != "stop" else f"Stopped {container_id}"
    return False, (out.stderr.strip().splitlines() or ["docker failed"])[-1]
