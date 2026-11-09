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
