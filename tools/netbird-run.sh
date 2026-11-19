#!/usr/bin/env bash
# fieldmap-server: what runs inside the NetBird container (the "netbird" service in the compose
# files, started by scripts/remote.sh netbird-apply; NetBird's own image, which has bash).
# NetBird is the other way in for paired devices, for a box whose router cannot open a port.
#
# It starts NetBird's client, joins the owner's NetBird network with the setup key the owner
# typed on the Remote access page, and then writes down every few seconds what the client says,
# for tools/remote-server.py to read:
#   /fieldmap/status.json   `netbird status --json`
#   /fieldmap/status.for    which request of the page this client was started for (apply-request)
#   /fieldmap/problem       why it could not join ("key" or "server", then NetBird's own words)
#   /fieldmap/key-check     "checked": the box joined with this setup key, so the key works;
#                           "unchecked": the box was a member already and NetBird did not look at
#                           the new key (a member that has its own identity does not need one)
# It reads /fieldmap/client.env (the management server) and /fieldmap/setup-key.
#
# The client is kept to the one thing the box needs, reaching and being reached at the gateway:
#   --interface-name nb-fieldmap   its own interface name; scripts/firewall.sh lets in the
#                                  gateway's port on it and nothing else
#   --disable-firewall             NetBird changes no firewall rule on the box
#   --disable-dns                  NetBird does not touch the box's name servers
#   --disable-client-routes        no traffic of the box is sent somewhere because the NetBird network says so
#   --disable-server-routes        the box never becomes a router into its own network
#   --block-lan-access             (the same, a second time over)
#   --disable-ipv6                 IPv4 inside the NetBird network only
# NetBird's SSH server and remote jobs are off unless asked for, and nothing here asks.
set -u
DIR=/fieldmap
NAME="${FIELDMAP_NETBIRD_NAME:-fieldmap-box}"
INTERFACE="${FIELDMAP_NETBIRD_INTERFACE:-nb-fieldmap}"
EVERY=5

# The management server, from client.env. Read as text, never run: only an address passes.
ADDRESS='^https?://[][A-Za-z0-9.:-]+$'
management() {
    local value
    value="$(grep -m1 '^NB_MANAGEMENT_URL=' "$DIR/client.env" 2>/dev/null | cut -d= -f2-)"
    if [[ "$value" =~ $ADDRESS ]]; then echo "$value"; fi
}

say() { echo "fieldmap: $*"; }
problem() { printf '%s\n%s\n' "$1" "$2" > "$DIR/problem.tmp" && mv "$DIR/problem.tmp" "$DIR/problem"; }

URL="$(management)"
cat "$DIR/apply-request" > "$DIR/status.for" 2>/dev/null
if [ -z "$URL" ] || [ ! -s "$DIR/setup-key" ]; then
    # Nothing to join: NetBird's client is not started at all (it would turn to NetBird's cloud).
    problem start "no management server or no setup key"
    say "no management server or no setup key: not started"
    sleep 300 &
    trap 'kill $! 2>/dev/null; exit 0' TERM INT
    wait $!
    exit 1
fi

# Everything goes to the console (docker logs), nothing to a file in the container. The client
# is told its server from the start: it never turns to another one, also not NetBird's cloud
# when the owner has an own server.
export NB_LOG_FILE=console NB_MANAGEMENT_URL="$URL"
netbird service run &
DAEMON=$!
trap 'kill -TERM "$DAEMON" 2>/dev/null; wait "$DAEMON" 2>/dev/null; exit 0' TERM INT

for _ in $(seq 1 30); do
    netbird status --check live >/dev/null 2>&1 && break
    sleep 1
done

# Joins (the first time, with the setup key) or connects again (after that the client has its
# own key, and the setup key is not used). Says why when it does not work.
up() {
    local url="$URL" out
    local sum
    sum="$(sha256sum < "$DIR/setup-key" | cut -c1-64)"
    if out="$(timeout 90 netbird up --management-url "$url" --setup-key-file "$DIR/setup-key" \
            --hostname "$NAME" --interface-name "$INTERFACE" \
            --disable-firewall --disable-dns --disable-client-routes --disable-server-routes \
            --block-lan-access --disable-ipv6 2>&1)"; then
        rm -f "$DIR/problem"
        # The first join is made with the key (/var/lib/netbird is new then): note which one.
        [ -s /var/lib/netbird/.joined-with ] || echo "$sum" > /var/lib/netbird/.joined-with
        if [ "$(cat /var/lib/netbird/.joined-with)" = "$sum" ]; then echo checked; else echo unchecked; fi > "$DIR/key-check"
        say "connected to $url"
        return 0
    fi
    # NetBird's words, without the key if it ever were in them, and short.
    out="$(printf '%s' "$out" | grep -vF -- "$(cat "$DIR/setup-key")" | tail -3 | cut -c1-300)"
    if printf '%s' "$out" | grep -qiE 'setup key|permission ?denied|unauthenticated|invalid'; then
        problem key "$out"
    else
        problem server "$out"
    fi
    say "could not connect to $url: $out"
    return 1
}

up
TRIED=$SECONDS
KEY_SEEN="$(stat -c %Y "$DIR/setup-key" 2>/dev/null)"

while kill -0 "$DAEMON" 2>/dev/null; do
    if netbird status --json > "$DIR/status.json.tmp" 2>/dev/null; then
        mv "$DIR/status.json.tmp" "$DIR/status.json"
    fi
    # Not joined yet: try again when the key changed, and now and then when the server did not
    # answer (a box that started before its internet connection). A key that was refused is not
    # sent again and again.
    if [ -e "$DIR/problem" ]; then
        KEY_NOW="$(stat -c %Y "$DIR/setup-key" 2>/dev/null)"
        if [ "$KEY_NOW" != "$KEY_SEEN" ] || { [ "$(head -1 "$DIR/problem")" != key ] && [ $((SECONDS - TRIED)) -ge 60 ]; }; then
            KEY_SEEN="$KEY_NOW"; TRIED=$SECONDS
            up
        fi
    fi
    sleep "$EVERY" &
    wait $!
done
say "the NetBird client stopped"
exit 1
