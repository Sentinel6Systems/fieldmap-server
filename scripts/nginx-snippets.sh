#!/usr/bin/env bash
# Puts fieldmap-server's nginx snippets (location blocks, made from pi/nginx/ by install-nginx.sh and
# scripts/remote.sh) into the folder that the web server's server block includes, and reloads
# nginx, all or nothing: only files that differ are written; if `nginx -t` then fails, every one
# of them is put back as it was, nginx is not reloaded, and this exits with an error.
#
# Usage (as root): scripts/nginx-snippets.sh <folder with the new snippets> <nginx folder>
# Prints "Changed: <file>" for each file it wrote (and adds it to $FIELDMAP_CHANGES, a file).
set -euo pipefail
SRC="${1:?usage: nginx-snippets.sh <folder with the new snippets> <nginx folder>}"
DEST="${2:?usage: nginx-snippets.sh <folder with the new snippets> <nginx folder>}"
[ -d "$DEST" ] || { echo "Error: the nginx folder $DEST does not exist." >&2; exit 1; }
command -v nginx >/dev/null || { echo "Error: nginx is not installed." >&2; exit 1; }

BACKUP="$(mktemp -d)"
trap 'rm -rf "$BACKUP"' EXIT
written=()
for new in "$SRC"/*.conf; do
    name="$(basename "$new")"
    cmp -s "$new" "$DEST/$name" && continue
    if [ -e "$DEST/$name" ]; then cp -p "$DEST/$name" "$BACKUP/$name"; fi
    install -m 644 "$new" "$DEST/$name"
    written+=("$name")
done
[ ${#written[@]} -gt 0 ] || exit 0

if ! out="$(nginx -t 2>&1)"; then
    for name in "${written[@]}"; do
        if [ -e "$BACKUP/$name" ]; then cp -p "$BACKUP/$name" "$DEST/$name"; else rm -f "$DEST/$name"; fi
    done
    echo "$out" >&2
    echo "Error: nginx refused its configuration with fieldmap-server's snippets in $DEST (above), so they are" >&2
    echo "       put back as they were. The snippets are location blocks: that folder must be included INSIDE a" >&2
    echo "       server block (include $DEST/*.conf;), not at http level like Debian's /etc/nginx/conf.d." >&2
    exit 1
fi
systemctl reload nginx
for name in "${written[@]}"; do
    echo "Changed: $DEST/$name"
    if [ -n "${FIELDMAP_CHANGES:-}" ]; then echo "$DEST/$name" >> "$FIELDMAP_CHANGES"; fi
done
