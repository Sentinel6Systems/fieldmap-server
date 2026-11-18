#!/usr/bin/env bash
# fieldmap-server: a firewall on the box itself, for remote access by the Fieldmap app.
# From outside the local network it lets in only the remote gateway, SSH, and answers to what
# the box asked for itself; from the local network, everything as before. It does not rely on
# the router: some routers let IPv6 in unfiltered, and a mistake in a port mapping should
# expose nothing.
#
# Usage (as root; scripts/remote.sh installs it as fieldmap-firewall, refreshed every five
# minutes because the home network's IPv6 prefix can change):
#   sudo ./scripts/firewall.sh apply      write the rules (again)
#   sudo ./scripts/firewall.sh show       the rules as they are now
#   sudo ./scripts/firewall.sh off        remove them
#
# It is its own nftables table, beside whatever else the machine has (Internet-in-a-Box's
# firewall, Docker's, ufw): it only ever adds a refusal, never an opening.
# Settings in .env: REMOTE_FIREWALL=off, REMOTE_PORT (default 8443), REMOTE_FIREWALL_ALLOW
# (ports open from outside besides the gateway, default 22; empty for none).
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

case "${1:-apply}" in
off)
    if command -v nft >/dev/null; then nft delete table $TABLE 2>/dev/null || true; fi
    exit 0
    ;;
show)
    nft list table $TABLE
    exit 0
    ;;
apply) ;;
*) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac

if [ "$(setting REMOTE_FIREWALL on)" = off ]; then exec "$0" off; fi
if ! command -v nft >/dev/null; then
    if command -v apt-get >/dev/null; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nftables >/dev/null
    elif command -v dnf >/dev/null; then
        dnf install -y -q nftables >/dev/null
    fi
fi
command -v nft >/dev/null || { echo "nftables is not installed: no firewall on this box." >&2; exit 0; }

PORT="$(setting REMOTE_PORT 8443)"
ALLOW="$(setting REMOTE_FIREWALL_ALLOW 22)"
# The Phones page's switch (data/remote/settings.json, "enabled"): off, the default, closes the gateway's port as well.
ON="$(python3 -c 'import json,sys
try: print("yes" if json.load(open(sys.argv[1])).get("enabled") is True else "no")
except Exception: print("no")' "$REPO_DIR/data/remote/settings.json" 2>/dev/null || echo no)"
if [ "$ON" = no ]; then PORT=""; GATEWAY_RULE="# remote access switched off: no gateway"; else GATEWAY_RULE="tcp dport 8443 accept"; fi
OPEN="$(echo "$PORT $ALLOW" | tr ', ' '\n\n' | grep -E '^[0-9]{1,5}$' | sort -un | paste -sd, - || true)"

# The IPv6 networks this machine is on: devices at home have global addresses in them.
PREFIXES="$(ip -j -6 route show 2>/dev/null | python3 -c '
import ipaddress, json, sys
out = set()
for r in json.load(sys.stdin or "[]"):
    dst, dev = r.get("dst", ""), r.get("dev", "")
    if "/" not in dst or dev.startswith(("docker", "br-", "veth", "tailscale", "wg", "zt")):
        continue
    try:
        net = ipaddress.ip_network(dst, strict=False)
    except ValueError:
        continue
    if not (net.is_link_local or net.is_multicast or net.is_loopback) and 32 <= net.prefixlen <= 64 and not r.get("gateway"):
        out.add(str(net))
print(", ".join(sorted(out)))' 2>/dev/null || true)"
LAN6="::1/128, fe80::/10, fc00::/7${PREFIXES:+, $PREFIXES}"

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
        ct state established,related accept
        meta l4proto { icmp, ipv6-icmp } accept
        iifname "tailscale0" accept
        iifname "wg*" accept
        ip saddr @lan4 accept
        ip6 saddr @lan6 accept
        tcp dport @open_tcp accept
        counter drop
    }

    # Containers (Docker) are reached through here, not through input.
    chain forward {
        type filter hook forward priority filter - 5; policy accept;
        ct state established,related accept
        meta l4proto { icmp, ipv6-icmp } accept
        iifname "tailscale0" accept
        iifname "wg*" accept
        ip saddr @lan4 accept
        ip6 saddr @lan6 accept
        $GATEWAY_RULE
        counter drop
    }
}
EOF
echo "Firewall on this box: from outside the local network only TCP ${OPEN:-(nothing)} get in${PORT:+ (the gateway is $PORT)}."
