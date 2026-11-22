#!/usr/bin/python3 -I
"""Stops other services of the machine while the Map Builder builds, to free RAM, and starts
them again afterwards.

    pause-services stop      stop the services that FIELDMAP_PAUSE_UNITS in .env names
    pause-services restore   start again what "stop" stopped (all of it, or what is left)
    pause-services status    the list, what it refuses and what is stopped now, as JSON

FIELDMAP_PAUSE_UNITS is a list the owner of the machine writes, separated by spaces or commas:
systemd services by name (jellyfin, calibre-web.service), Docker containers as docker:<name>
(docker:kiwix). Nothing else is ever stopped. Services the machine cannot do without are
refused even when the list names them (NEVER below): the web server, SSH, the network, Docker
itself, and fieldmap-server's own services.

Before it stops a service, "stop" adds it to a state file (STATE, root's, in
/var/lib/fieldmap-server), and "restore" starts again only what that file lists. So whatever
ends a build, the services come back: the Map Builder runs "restore" when a build finishes,
fails or is canceled, and when it finds a build that died (tools/admin-server.py);
fieldmap-build-restore.service runs it at every boot, for a build that a power cut or a reboot
ended.

Why root, and how: only root may stop a system service. The Map Builder runs as an ordinary
user. The installers (scripts/pause-setup.sh) put a copy of this file, owned by root, at
/usr/local/lib/fieldmap-server/pause-services, and add one sudoers rule that lets the Map
Builder's user run exactly that file with exactly "stop" or "restore": no other program, no
other argument, and a file that user cannot change. That is narrower than a group or a
polkit rule for systemctl, and simpler than a root service asked through a request file: the
build waits for the answer and its log shows what was stopped. The names come only from .env,
and "restore" starts only what "stop" stopped itself, as the state file is root's.
Python's standard library only. Started with -I: no module or setting of the caller's is read.
"""
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The installed copy has this line set to the repository's .env (scripts/pause-setup.sh).
ENV_FILE = os.path.join(ROOT, ".env")
STATE = "/var/lib/fieldmap-server/paused.json"
SETTING = "FIELDMAP_PAUSE_UNITS"

# Never stopped, whatever the list says: without them the machine cannot be reached, the maps
# stop, or the build itself cannot run. Names without ".service"; a "*" ends a prefix.
NEVER = ["nginx", "apache2", "httpd", "lighttpd", "caddy", "ssh", "sshd", "dropbear", "docker", "containerd",
         "dbus", "dbus-broker", "polkit", "systemd-*", "networking", "NetworkManager", "network-manager",
         "wpa_supplicant", "dhcpcd", "isc-dhcp-*", "hostapd", "dnsmasq", "avahi-daemon", "tailscaled",
         "netbird", "zerotier-one", "wireguard", "wg-quick@*", "ufw", "nftables", "firewalld", "iptables",
         "cron", "rsyslog", "getty@*", "serial-getty@*", "user@*", "sudo", "fieldmap*", "tileserver*",
         "box-firewall*"]
NEVER_CONTAINERS = ["tileserver", "fieldmap*"]
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._:-]{0,200}")


def _matches(name, patterns):
    return any(name == p or (p.endswith("*") and name.startswith(p[:-1])) for p in patterns)


def read_setting(env_file=None):
    """The raw value of FIELDMAP_PAUSE_UNITS in .env ("" when there is none). The environment
    is not read: the root copy must take the list only from the owner's file."""
    try:
        with open(env_file or ENV_FILE) as f:
            lines = [l.strip() for l in f if l.startswith(SETTING + "=")]
    except OSError:
        return ""
    if not lines:
        return ""
    value = lines[-1].split("=", 1)[1].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def parse(value):
    """(services, refused) of a FIELDMAP_PAUSE_UNITS value. A service is
    {"kind": "unit" | "container", "name": ..., "label": ...}; refused is [{"name", "why"}]."""
    services, refused, seen = [], [], set()
    for raw in re.split(r"[\s,]+", value.strip()):
        if not raw:
            continue
        kind, name = ("container", raw[7:]) if raw.startswith("docker:") else ("unit", raw)
        if not NAME.fullmatch(name):
            refused.append({"name": raw, "why": "not a valid service name"})
            continue
        if kind == "unit":
            base = name[:-8] if name.endswith(".service") else name
            if "." in base:
                refused.append({"name": raw, "why": "only services (.service) can be stopped"})
                continue
            if _matches(base, NEVER):
                refused.append({"name": raw, "why": "the server needs it"})
                continue
            name, label = base + ".service", base
        else:
            if _matches(name, NEVER_CONTAINERS):
                refused.append({"name": raw, "why": "the server needs it"})
                continue
            label = name
        if (kind, name) not in seen:
            seen.add((kind, name))
            services.append({"kind": kind, "name": name, "label": label})
    return services, refused


def configured(env_file=None):
    return parse(read_setting(env_file))


def run_command(args):
    r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return r.returncode, r.stdout.strip()


def is_running(service, run=run_command):
    if service["kind"] == "unit":
        return run(["systemctl", "is-active", "--quiet", service["name"]])[0] == 0
    code, out = run(["docker", "inspect", "-f", "{{.State.Running}}", service["name"]])
    return code == 0 and out == "true"


def stop_command(service):
    return (["systemctl", "stop", service["name"]] if service["kind"] == "unit"
            else ["docker", "stop", service["name"]])


def start_command(service):
    return (["systemctl", "start", service["name"]] if service["kind"] == "unit"
            else ["docker", "start", service["name"]])


def read_state(path=STATE):
    try:
        with open(path) as f:
            value = json.load(f)
        stopped = [s for s in value.get("stopped", []) if isinstance(s, dict) and s.get("kind") in ("unit", "container")
                   and isinstance(s.get("name"), str) and NAME.fullmatch(s["name"])]
        return {"since": value.get("since"), "stopped": stopped}
    except (OSError, ValueError, AttributeError):
        return {"since": None, "stopped": []}


def write_state(state, path=STATE):
    """Written whole and then renamed into place, readable by everyone (the Map Builder page
    shows it), writable by root only. No state file: nothing stopped."""
    if not state["stopped"]:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def stop(env_file=None, path=STATE, run=run_command, say=print):
    """Stops the running services of the list, each noted in the state file before it is
    stopped (a crash in between leaves one more to start, never one less). Services stopped by
    an earlier build that were not started again stay in the file. Returns 0, or 1 when one
    could not be stopped (the others stay stopped; "restore" starts them all)."""
    services, refused = configured(env_file)
    for r in refused:
        say(f"Not stopped: {r['name']} ({r['why']}).")
    state = read_state(path)
    state["since"] = state["since"] or int(time.time())
    code = 0
    for s in services:
        if not is_running(s, run):
            say(f"Not running: {s['label']}.")
            continue
        entry = {"kind": s["kind"], "name": s["name"], "label": s["label"]}
        if not any(e["kind"] == s["kind"] and e["name"] == s["name"] for e in state["stopped"]):
            state["stopped"].append(entry)
            write_state(state, path)
        rc, out = run(stop_command(s))
        if rc == 0:
            say(f"Stopped: {s['label']}.")
        else:
            say(f"Could not stop {s['label']}: {out or 'error ' + str(rc)}")
            code = 1
    return code


def restore(path=STATE, run=run_command, say=print):
    """Starts again, last stopped first, every service the state file lists, and takes each
    one that started off the list (the file goes when the list is empty). Returns 0, or 1 when
    one did not start (it stays on the list for the next try)."""
    state = read_state(path)
    left = []
    for s in reversed(state["stopped"]):
        rc, out = run(start_command(s))
        if rc == 0:
            say(f"Started again: {s.get('label') or s['name']}.")
        else:
            say(f"Could not start {s.get('label') or s['name']}: {out or 'error ' + str(rc)}")
            left.insert(0, s)
    state["stopped"] = left
    write_state(state, path)
    return 1 if left else 0


def status(env_file=None, path=STATE):
    services, refused = configured(env_file)
    return {"services": services, "refused": refused, "stopped": read_state(path)["stopped"]}


def main(argv):
    if argv == ["status"]:
        print(json.dumps(status()))
        return 0
    if argv not in (["stop"], ["restore"]):
        sys.exit(__doc__)
    if os.geteuid() != 0:
        sys.exit("Error: run this as root (the Map Builder runs it with sudo; see scripts/pause-setup.sh).")
    return stop() if argv == ["stop"] else restore()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
