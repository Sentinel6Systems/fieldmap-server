#!/usr/bin/env bash
# Lets the Map Builder stop the services that FIELDMAP_PAUSE_UNITS in .env names while it builds,
# and start them again (tools/pause-services.py says what it does and why it is done this way).
# The installers run this, as root:
#   sudo scripts/pause-setup.sh <user>
# <user> is the user the Map Builder runs as. It installs:
#   /usr/local/lib/fieldmap-server/pause-services   a copy of tools/pause-services.py, owned by root,
#                                                   which reads the list from this folder's .env
#   /etc/sudoers.d/fieldmap-pause                   one rule: <user> may run exactly that file, with
#                                                   exactly "stop" or "restore", as root
#   fieldmap-build-restore.service                  at every boot, starts again what a build that
#                                                   a reboot or a power cut ended had stopped
# Prints "Changed: ..." for what it wrote (and adds it to $FIELDMAP_CHANGES, a file). A second run
# with nothing new changes nothing.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${1:?usage: pause-setup.sh <user the Map Builder runs as>}"
[ "$(id -u)" -eq 0 ] || { echo "Error: run this as root." >&2; exit 1; }
id "$USER_NAME" >/dev/null 2>&1 || { echo "Error: no user $USER_NAME." >&2; exit 1; }
changed() { echo "Changed: $1"; if [ -n "${FIELDMAP_CHANGES:-}" ]; then echo "$1" >> "$FIELDMAP_CHANGES"; fi; }
HELPER=/usr/local/lib/fieldmap-server/pause-services
SUDOERS=/etc/sudoers.d/fieldmap-pause
UNIT=/etc/systemd/system/fieldmap-build-restore.service
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# The copy reads this folder's .env, wherever the copy is.
sed "s#^ENV_FILE = .*#ENV_FILE = \"$REPO/.env\"#" "$REPO/tools/pause-services.py" > "$TMP/helper"
if ! cmp -s "$TMP/helper" "$HELPER"; then
    install -d -m 755 -o root -g root "$(dirname "$HELPER")"
    install -m 755 -o root -g root "$TMP/helper" "$HELPER"
    changed "$HELPER"
fi

cat > "$TMP/sudoers" <<EOF
# fieldmap-server (scripts/pause-setup.sh): the Map Builder (user $USER_NAME) stops the services that
# FIELDMAP_PAUSE_UNITS in $REPO/.env names while it builds, and starts them again. This file and
# nothing else, with these two arguments only.
$USER_NAME ALL=(root) NOPASSWD: $HELPER stop, $HELPER restore
EOF
if ! cmp -s "$TMP/sudoers" "$SUDOERS"; then
    visudo -cqf "$TMP/sudoers" || { echo "Error: the sudoers rule for the Map Builder is not valid; nothing written." >&2; exit 1; }
    install -m 440 -o root -g root "$TMP/sudoers" "$SUDOERS"
    changed "$SUDOERS"
fi

cat > "$TMP/unit" <<EOF
# fieldmap-server (scripts/pause-setup.sh): after a reboot, start again the services that a Map
# Builder build stopped (FIELDMAP_PAUSE_UNITS) and that a reboot or a power cut kept it from
# starting again. Does nothing when no build stopped anything.
[Unit]
Description=fieldmap-server: start the services a Map Builder build stopped
After=docker.service local-fs.target
Wants=docker.service

[Service]
Type=oneshot
ExecStart=$HELPER restore

[Install]
WantedBy=multi-user.target
EOF
if ! cmp -s "$TMP/unit" "$UNIT"; then
    install -m 644 "$TMP/unit" "$UNIT"
    systemctl daemon-reload
    changed "$UNIT"
fi
systemctl is-enabled -q fieldmap-build-restore.service 2>/dev/null || {
    systemctl enable -q fieldmap-build-restore.service
    changed "enabled fieldmap-build-restore.service"
}
