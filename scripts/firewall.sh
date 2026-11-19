#!/usr/bin/env bash
# fieldmap-server: a firewall on the box itself. Only the local network and VPNs get in.
# From anywhere else nothing gets an answer: no reply to a ping, no "closed" for a port that is
# closed, nothing. Packets from outside are dropped without a word, so that a scan finds no box
# at all. The two exceptions: answers to what the box asked for itself, and the remote gateway
# while remote access is switched on (Admin, Remote access), which must take phones from anywhere.
# It does not rely on the router: some routers let IPv6 in unfiltered, and a mistake in a port
# mapping should expose nothing. SSH from outside is closed too, unless .env opens it.
#
# Usage (as root; the installers run "install": it is on whether or not remote access is set
# up, refreshed every five minutes because the home network's IPv6 prefix can change):
#   sudo ./scripts/firewall.sh install    install it as fieldmap-firewall (a systemd timer) and apply it
#   sudo ./scripts/firewall.sh apply      write the rules (again)
#   sudo ./scripts/firewall.sh show       the rules as they are now
#   sudo ./scripts/firewall.sh off        remove them (until the next apply: the timer brings them back)
#
# It is its own nftables table, beside whatever else the machine has (Internet-in-a-Box's
# firewall, Docker's, ufw). The one opening it makes elsewhere: while remote access is on, the
# gateway's port in iptables (see below).
# Settings in .env: REMOTE_FIREWALL=off (no firewall: for a machine that serves other things to
# the internet), REMOTE_PORT (written by scripts/remote.sh setup), REMOTE_FIREWALL_ALLOW (ports
# open from outside besides the gateway: none by default; 22 to reach SSH from outside).
set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TABLE="inet fieldmap_guard"

# A setting from .env; the default when .env does not name it. Named but empty stays empty.
setting() {
    local line
    line="$(grep -E "^$1=" "$REPO_DIR/.env" 2>/dev/null | tail -1 || true)"
    [ -n "$line" ] || { echo "$2"; return; }
    line="${line#*=}"
    line="${line%\"}"; line="${line#\"}"; line="${line%\'}"; line="${line#\'}"
    echo "$line"
}
[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }

# Another firewall on the machine (Internet-in-a-Box's iiab-gen-iptables, ufw) may drop new
# connections from the home network, which for a box plugged into a home router is its "WAN"
# side. Then the gateway's port is let through it as well, by one rule of ours (marked with a
# comment so that it can be found and taken out again), and only while remote access is on.
# Re-applied with the rest every five minutes: Internet-in-a-Box rewrites its rules now and then.
MARK="fieldmap-remote"
iptables_close() {
    command -v iptables >/dev/null || return 0
    local rule
    { iptables -S INPUT 2>/dev/null || true; } | { grep -- "--comment $MARK" || true; } | sed 's/^-A /-D /' |
        while read -r rule; do eval "iptables $rule" 2>/dev/null || true; done
}
iptables_open() {
    command -v iptables >/dev/null || return 0
    iptables_close
    if iptables -I INPUT 1 -p tcp --dport "$1" -m comment --comment "$MARK" -j ACCEPT 2>/dev/null; then
        echo "Also let through this machine's other firewall: TCP $1."
    else
        echo "Could not add TCP $1 to this machine's other firewall (iptables)." >&2
    fi
}

case "${1:-apply}" in
install)
    if ! command -v systemctl >/dev/null || [ ! -d /run/systemd/system ]; then
        echo "This machine has no systemd: the firewall is applied once now, not kept up. Run this again after a restart." >&2
    else
        for unit in fieldmap-firewall.service fieldmap-firewall.timer fieldmap-firewall.path; do
            sed "s|/opt/fieldmap-server|$REPO_DIR|g" "$REPO_DIR/systemd/$unit" > "/etc/systemd/system/$unit"
        done
        systemctl daemon-reload
        if [ "$(setting REMOTE_FIREWALL on)" = off ]; then
            systemctl disable -q --now fieldmap-firewall.timer fieldmap-firewall.path 2>/dev/null || true
        else
            systemctl enable -q --now fieldmap-firewall.timer fieldmap-firewall.path
        fi
    fi
    # Applied here as well as by the service, so that what it says is printed.
    ;;
off)
    if command -v nft >/dev/null; then nft delete table $TABLE 2>/dev/null || true; fi
    iptables_close
    exit 0
    ;;
show)
    nft list table $TABLE
    exit 0
    ;;
apply) ;;
*) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac

PORT="$(setting REMOTE_PORT "")"
# The Remote access page's switch (data/remote/settings.json, "enabled"): off, the default, closes the gateway's port as well.
ON="$(python3 -c 'import json,sys
try: print("yes" if json.load(open(sys.argv[1])).get("enabled") is True else "no")
except Exception: print("no")' "$REPO_DIR/data/remote/settings.json" 2>/dev/null || echo no)"
# Remote access taken off the box (remote.sh off, install.sh --no-remote): no gateway listens, and
# an open port with nothing behind it would answer "closed".
[ -e "$REPO_DIR/data/remote/run/no-remote" ] && ON=no
if [ "$ON" = yes ] && [ -n "$PORT" ]; then iptables_open "$PORT"; else iptables_close; fi

if [ "$(setting REMOTE_FIREWALL on)" = off ]; then
    nft delete table $TABLE 2>/dev/null || true
    echo "No firewall of its own on this box (REMOTE_FIREWALL=off)."
    exit 0
fi
if ! command -v nft >/dev/null; then
    if command -v apt-get >/dev/null; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nftables >/dev/null
    elif command -v dnf >/dev/null; then
        dnf install -y -q nftables >/dev/null
    fi
fi
command -v nft >/dev/null || { echo "nftables is not installed: no firewall on this box." >&2; exit 0; }

ALLOW="$(setting REMOTE_FIREWALL_ALLOW "")"
if [ "$ON" = no ]; then PORT=""; GATEWAY_RULE="# remote access switched off: no gateway"; else GATEWAY_RULE="tcp dport 8443 accept"; fi
OPEN="$(echo "$PORT $ALLOW" | tr ', ' '\n\n' | grep -E '^[0-9]{1,5}$' | sort -un | paste -sd, - || true)"

# The IPv6 networks this machine is on: devices at home have global addresses in them. Put
# together with the local ranges into one list without overlaps: nft refuses a set in which one
# network lies inside another, and a home network's own fd.. prefix lies inside fc00::/7.
LAN6="$(ip -j -6 route show 2>/dev/null | python3 -c '
import ipaddress, json, sys
out = [ipaddress.ip_network(n) for n in ("::1/128", "fe80::/10", "fc00::/7")]
try:
    routes = json.load(sys.stdin)
except ValueError:
    routes = []
for r in routes:
    dst, dev = r.get("dst", ""), r.get("dev", "")
    if "/" not in dst or dev.startswith(("docker", "br-", "veth", "tailscale", "wg", "zt")):
        continue
    try:
        net = ipaddress.ip_network(dst, strict=False)
    except ValueError:
        continue
    if not (net.is_link_local or net.is_multicast or net.is_loopback) and 32 <= net.prefixlen <= 64 and not r.get("gateway"):
        out.append(net)
print(", ".join(str(n) for n in ipaddress.collapse_addresses(out)))' 2>/dev/null || true)"
LAN6="${LAN6:-::1/128, fe80::/10, fc00::/7}"

nft -f - <<EOF
table $TABLE
delete table $TABLE
table $TABLE {
    set lan4 {
        type ipv4_addr; flags interval
        elements = { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16, 127.0.0.0/8 }
    }
    set lan6 {
        type ipv6_addr; flags interval
        elements = { $LAN6 }
    }
    set open_tcp {
        type inet_service
${OPEN:+        elements = { $OPEN \}}
    }

    chain input {
        type filter hook input priority filter - 5; policy accept;
        iif "lo" accept
        # Answers to what the box asked for itself, and the error messages that belong to them.
        ct state established,related accept
        # IPv6's own housekeeping with the router and the devices on the same wire. A hop limit of
        # 255 proves the packet was not forwarded by any router: it comes from the same network.
        icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert, nd-router-solicit, nd-router-advert, nd-redirect } ip6 hoplimit 255 accept
        # VPNs: Tailscale, WireGuard, ZeroTier, OpenVPN.
        iifname "tailscale0" accept
        iifname "wg*" accept
        iifname "zt*" accept
        iifname "tun*" accept
        # The local network: everything, pings included.
        ip saddr @lan4 accept
        ip6 saddr @lan6 accept
        tcp dport @open_tcp accept
        # Everything else is dropped without an answer (drop, not reject): not even a ping.
        counter drop
    }

    # Containers (Docker) are reached through here, not through input.
    chain forward {
        type filter hook forward priority filter - 5; policy accept;
        ct state established,related accept
        iifname "tailscale0" accept
        iifname "wg*" accept
        iifname "zt*" accept
        iifname "tun*" accept
        ip saddr @lan4 accept
        ip6 saddr @lan6 accept
        $GATEWAY_RULE
        counter drop
    }
}
EOF
if [ -n "$OPEN" ]; then
    echo "Firewall on this box: from outside the local network and VPNs it lets in TCP $OPEN only${PORT:+ (the gateway is $PORT)}; everything else is dropped without an answer."
else
    echo "Firewall on this box: from outside the local network and VPNs nothing gets in, and nothing gets an answer."
fi
if ! echo ",$OPEN," | grep -q ",22,"; then
    echo "SSH from outside the local network is closed (from this network it works as before). To open it: REMOTE_FIREWALL_ALLOW=22 in .env."
fi
