#!/bin/bash
# install.sh — Soft-recovery + diagnostics for Dialog SIP trunk.
# Idempotent: backs up originals to *.bak-recover-<ts> before overwriting.
# Run with sudo: sudo bash /home/horapusa/voip-recovery-staging/install.sh
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "must be run as root (use sudo)" >&2
  exit 1
fi

STAGING=/home/horapusa/voip-recovery-staging
TS=$(date -u +%Y%m%dT%H%M%SZ)
echo "==> staging dir: $STAGING"
echo "==> backup tag:  bak-recover-$TS"

# --- 0. Pre-flight ---
for f in app.py trunk.html bridge.ts snapshot.sh voip-probe.sh \
         voip-probe.service voip-probe.timer sudoers-extra \
         logrotate-pbx-monitor cron-snapshot-prune; do
  [ -f "$STAGING/$f" ] || { echo "missing staging file: $f" >&2; exit 1; }
done

# Validate sudoers fragment BEFORE installing (visudo dry-run).
visudo -cf "$STAGING/sudoers-extra" >/dev/null
echo "==> sudoers fragment is valid"

# Validate Python syntax of new app.py.
python3 -m py_compile "$STAGING/app.py"
echo "==> app.py compiles"

# --- 1. /var/log/pbx-monitor + snapshots dir ---
install -d -o asterisk -g asterisk -m 0755 /var/log/pbx-monitor
install -d -o asterisk -g asterisk -m 0755 /var/log/pbx-monitor/snapshots
echo "==> /var/log/pbx-monitor ready"

# --- 2. Snapshot + probe scripts ---
install -o root     -g root     -m 0755 "$STAGING/snapshot.sh"   /opt/pbx-monitor/snapshot.sh
install -o asterisk -g asterisk -m 0755 "$STAGING/voip-probe.sh" /opt/pbx-monitor/voip-probe.sh
echo "==> snapshot.sh + voip-probe.sh installed"

# --- 3. sudoers fragment (appended to existing /etc/sudoers.d/pbx-monitor) ---
# Idempotent: only append the marker-block if not already present.
MARKER_BEGIN="# >>> soft-recover additions"
MARKER_END="# <<< soft-recover additions"
SUDOFILE=/etc/sudoers.d/pbx-monitor
if grep -qF "$MARKER_BEGIN" "$SUDOFILE" 2>/dev/null; then
  echo "==> sudoers additions already present — skipping"
else
  cp -a "$SUDOFILE" "$SUDOFILE.bak-recover-$TS" 2>/dev/null || true
  {
    echo ""
    echo "$MARKER_BEGIN"
    cat "$STAGING/sudoers-extra"
    echo "$MARKER_END"
  } >> "$SUDOFILE"
  chmod 0440 "$SUDOFILE"
  visudo -cf "$SUDOFILE" >/dev/null
  echo "==> sudoers extended"
fi

# --- 4. logrotate config ---
install -o root -g root -m 0644 "$STAGING/logrotate-pbx-monitor" /etc/logrotate.d/pbx-monitor
echo "==> logrotate config installed"

# --- 5. Snapshot prune cron (daily at 04:17 UTC) ---
install -o root -g root -m 0644 "$STAGING/cron-snapshot-prune" /etc/cron.d/pbx-monitor-snapshot-prune
echo "==> snapshot prune cron installed"

# --- 6. voip-probe systemd unit + timer ---
install -o root -g root -m 0644 "$STAGING/voip-probe.service" /etc/systemd/system/voip-probe.service
install -o root -g root -m 0644 "$STAGING/voip-probe.timer"   /etc/systemd/system/voip-probe.timer
systemctl daemon-reload
systemctl enable --now voip-probe.timer
echo "==> voip-probe.timer enabled"

# --- 7. /opt/pbx-monitor/app.py (with backup) ---
if ! cmp -s "$STAGING/app.py" /opt/pbx-monitor/app.py; then
  cp -a /opt/pbx-monitor/app.py "/opt/pbx-monitor/app.py.bak-recover-$TS"
  install -o asterisk -g asterisk -m 0644 "$STAGING/app.py" /opt/pbx-monitor/app.py
  echo "==> app.py replaced (backup: app.py.bak-recover-$TS)"
else
  echo "==> app.py unchanged — skipping"
fi

# --- 8. /opt/pbx-monitor/templates/trunk.html (with backup) ---
if ! cmp -s "$STAGING/trunk.html" /opt/pbx-monitor/templates/trunk.html; then
  cp -a /opt/pbx-monitor/templates/trunk.html "/opt/pbx-monitor/templates/trunk.html.bak-recover-$TS"
  install -o asterisk -g asterisk -m 0644 "$STAGING/trunk.html" /opt/pbx-monitor/templates/trunk.html
  echo "==> trunk.html replaced (backup: trunk.html.bak-recover-$TS)"
else
  echo "==> trunk.html unchanged — skipping"
fi

# --- 9. /opt/sampath-ai/bridge.ts (with backup + tsc check) ---
if ! cmp -s "$STAGING/bridge.ts" /opt/sampath-ai/bridge.ts; then
  cp -a /opt/sampath-ai/bridge.ts "/opt/sampath-ai/bridge.ts.bak-recover-$TS"
  install -o asterisk -g asterisk -m 0644 "$STAGING/bridge.ts" /opt/sampath-ai/bridge.ts
  # Best-effort TypeScript check (don't abort install if tsc missing/strict).
  if [ -x /opt/sampath-ai/node_modules/.bin/tsc ]; then
    if ! sudo -u asterisk /opt/sampath-ai/node_modules/.bin/tsc --noEmit --project /opt/sampath-ai/tsconfig.json 2>&1 | tee /tmp/tsc-recover.log | grep -q 'bridge.ts'; then
      echo "==> bridge.ts type-check OK"
    else
      echo "!! bridge.ts type-check produced warnings — see /tmp/tsc-recover.log"
    fi
  fi
  echo "==> bridge.ts replaced (backup: bridge.ts.bak-recover-$TS)"
else
  echo "==> bridge.ts unchanged — skipping"
fi

# --- 10. Restart services so changes take effect ---
echo "==> restarting pbx-monitor (UI changes)"
systemctl restart pbx-monitor

echo "==> restarting sampath-ai (watchdog changes)"
systemctl restart sampath-ai

# Note: we deliberately do NOT restart asterisk here.

# --- 11. Verification ---
sleep 2
echo "----------------------------------------------------------------"
echo "Status:"
systemctl --no-pager --lines=0 status pbx-monitor sampath-ai voip-probe.timer | grep -E "Active|Loaded|Trigger" | head -20
echo "----------------------------------------------------------------"
echo "Probe log will start appearing here within ~30s of first timer fire:"
echo "  tail -f /var/log/pbx-monitor/probe.log"
echo
echo "Soft-recover endpoint (admin role required):"
echo "  curl -X POST http://127.0.0.1:5051/api/soft-recover -H 'Cookie: ...'"
echo "Or click the new 'Recover trunk' button on the /trunk panel."
echo
echo "Snapshots will be written to /var/log/pbx-monitor/snapshots/<ts>_<label>/"
echo "----------------------------------------------------------------"
echo "DONE."
