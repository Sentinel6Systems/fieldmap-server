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

## The map builder page (optional)

`fieldmap-admin.service` keeps the map builder page (`scripts/admin.sh`, see the main README)
running across reboots. Copy it to `/etc/systemd/system/`, set `WorkingDirectory`, `User` and
the path in `ExecStart` to your clone and its owner, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fieldmap-admin
```

To make the page ask for a code, add `Environment=ADMIN_PIN=<code>` to the `[Service]` section.
