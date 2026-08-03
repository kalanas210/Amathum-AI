#!/bin/bash
# uninstall.sh — Roll back the soft-recovery install.
# Restores the most recent *.bak-recover-* backups, removes added units, sudoers block, cron, scripts.
# Run with sudo: sudo bash /home/horapusa/voip-recovery-staging/uninstall.sh
set -uo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "must be run as root (use sudo)" >&2
  exit 1
fi

# --- Disable + remove timer/service ---
systemctl disable --now voip-probe.timer 2>/dev/null || true
rm -f /etc/systemd/system/voip-probe.timer /etc/systemd/system/voip-probe.service
systemctl daemon-reload
echo "==> voip-probe units removed"

# --- Remove cron + logrotate ---
rm -f /etc/cron.d/pbx-monitor-snapshot-prune /etc/logrotate.d/pbx-monitor
echo "==> cron + logrotate removed"

# --- Restore most-recent backup of each modified file ---
restore() {
  local target="$1"
  local newest
  newest=$(ls -1t "${target}.bak-recover-"* 2>/dev/null | head -1 || true)
  if [ -n "$newest" ]; then
    cp -a "$newest" "$target"
    echo "==> restored $target from $(basename "$newest")"
  else
    echo "!! no backup found for $target — leaving in place"
  fi
}
restore /opt/pbx-monitor/app.py
restore /opt/pbx-monitor/templates/trunk.html
restore /opt/sampath-ai/bridge.ts

# --- Remove added scripts ---
rm -f /opt/pbx-monitor/snapshot.sh /opt/pbx-monitor/voip-probe.sh
echo "==> snapshot.sh + voip-probe.sh removed"

# --- Strip the marker-block from sudoers ---
SUDOFILE=/etc/sudoers.d/pbx-monitor
if [ -f "$SUDOFILE" ] && grep -qF "# >>> soft-recover additions" "$SUDOFILE"; then
  TMP=$(mktemp)
  awk '
    /# >>> soft-recover additions/ {skip=1; next}
    /# <<< soft-recover additions/ {skip=0; next}
    !skip {print}
  ' "$SUDOFILE" > "$TMP"
  visudo -cf "$TMP" >/dev/null && install -o root -g root -m 0440 "$TMP" "$SUDOFILE"
  rm -f "$TMP"
  echo "==> sudoers block stripped"
fi

# --- Restart UI + bridge so the old code is loaded ---
systemctl restart pbx-monitor sampath-ai
echo "==> services restarted"

echo "DONE. Snapshots + probe.log under /var/log/pbx-monitor/ are NOT deleted; remove manually if you want."
