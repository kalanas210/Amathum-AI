# Deploying Naxter Automations on port 5056

The app is a Next.js production server. It binds the port directly (no nginx
needed). Port 5056 is > 1024, so it does **not** require root to run — only the
systemd install steps below need `sudo`.

## 1. Build (already done; re-run after code changes)

```bash
cd /home/horapusa/ryzera/automation/web
npm ci            # first time / after dependency changes
npm run build
```

## 2. Install as a systemd service (durable, survives reboot)

```bash
# stop any interim copy first (frees port 5056)
fuser -k 5056/tcp 2>/dev/null || true

sudo cp /home/horapusa/ryzera/automation/web/deploy/naxter-automations.service \
        /etc/systemd/system/naxter-automations.service
sudo systemctl daemon-reload
sudo systemctl enable --now naxter-automations.service

# verify
systemctl status naxter-automations.service --no-pager
curl -s -o /dev/null -w "%{http_code}\n" http://100.68.210.114:5056/automations   # -> 200
```

Logs: `journalctl -u naxter-automations -f`

## 3. Updating later

```bash
cd /home/horapusa/ryzera/automation/web && git pull && npm ci && npm run build
sudo systemctl restart naxter-automations.service
```

## Notes

- **Bind / exposure:** the unit binds `100.68.210.114` (Tailscale only). The app
  has **no built-in auth**, so avoid `-H 0.0.0.0` unless a firewall restricts
  5056. To front it with nginx instead, bind `127.0.0.1` and proxy.
- **HTTP Request node = SSRF by design:** like n8n (and the original Flask app),
  the `httpRequest` node will fetch any URL the workflow author supplies,
  including `localhost`/internal/Tailnet addresses. Keep the app behind Tailscale
  (or add auth) so only trusted users can author workflows.
- **Data:** workflows live in `web/data/` (git-ignored). Override with
  `Environment=AUTOMATIONS_DATA_DIR=/path` in the unit. The two existing
  workflows were migrated here from the old Flask app.
- The old automations server was already stopped; the `pbx-monitor*` services
  (127.0.0.1:5050–5055) are separate and untouched.
