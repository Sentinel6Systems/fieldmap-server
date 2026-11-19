# Start the server at boot (optional)

Docker Compose restarts the container after a reboot if Docker starts.
This is the default (`restart: unless-stopped`).
Use this systemd unit if you want systemd to control the stack.

## Install

1. Copy the unit file:

   ```bash
   sudo cp systemd/tileserver.service /etc/systemd/system/
   ```

2. Edit `/etc/systemd/system/tileserver.service`.
   Set `WorkingDirectory` to the path of your clone.

3. Enable and start the unit:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now tileserver
   ```

## Useful commands

```bash
sudo systemctl status tileserver
sudo journalctl -u tileserver -f
docker logs -f tileserver
```

## The Map Builder page

`./scripts/install.sh` installs `fieldmap-admin.service` for you, with this folder and its owner
filled in, and starts it; `./scripts/install.sh --no-builder` removes it again. By hand: copy the
file to `/etc/systemd/system/`, set `WorkingDirectory`, `User` and the path in `ExecStart` to your
clone and its owner, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fieldmap-admin
```

To make the page ask for a code, set `ADMIN_PIN=<code>` in `.env` and restart the service.

## Remote access for the Android app

`./scripts/remote.sh setup` (run by the installers) installs these with this folder and its owner filled in:
`fieldmap-remote.service` (pairing, the list of paired devices, the router's port), `fieldmap-share.service` (positions and
markers shared between devices; it may write only `data/share/`), `fieldmap-firewall.timer` (the server's
own firewall, refreshed every five minutes; installed by every install, also without remote access, through
`scripts/firewall.sh install`; `fieldmap-firewall.service` also runs once before Docker at every start),
`fieldmap-remote-update.timer` (the weekly update of the gateway, and of NetBird's client where it runs) and
`fieldmap-netbird.path` (starts or removes NetBird's container when the Remote Access page asks for it). The
service reads `REMOTE_*` from `.env`; run `./scripts/remote.sh setup` again after changing them.
