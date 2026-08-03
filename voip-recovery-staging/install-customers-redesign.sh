#!/bin/bash
# install-customers-redesign.sh — replace the flat /customers table with the
# session-grouped card view. Adds /api/sessions endpoints.
# Idempotent: backs up originals as *.bak-custredesign-<ts>.
set -euo pipefail

if [ "$EUID" -ne 0 ]; then echo "must be root (use sudo)" >&2; exit 1; fi

STAGING=/home/horapusa/voip-recovery-staging
TS=$(date -u +%Y%m%dT%H%M%SZ)

for f in app.py flows/v2/templates/customers.html flows/v2/static/customers.js; do
  [ -f "$STAGING/$f" ] || { echo "missing: $STAGING/$f" >&2; exit 1; }
done
python3 -c "import ast; ast.parse(open('$STAGING/app.py').read())"
echo "==> app.py compiles"

for f in app.py:/opt/pbx-monitor/app.py \
         flows/v2/templates/customers.html:/opt/pbx-monitor/templates/customers.html \
         flows/v2/static/customers.js:/opt/pbx-monitor/static/customers.js
do
  src="$STAGING/${f%%:*}"; dst="${f##*:}"
  if [ -f "$dst" ] && ! cmp -s "$src" "$dst"; then
    cp -a "$dst" "$dst.bak-custredesign-$TS"
    install -o asterisk -g asterisk -m 0644 "$src" "$dst"
    echo "==> ${f##*:} updated (backup: $(basename "$dst").bak-custredesign-$TS)"
  else
    echo "==> ${f##*:} unchanged"
  fi
done

systemctl restart pbx-monitor
sleep 1
systemctl is-active pbx-monitor

echo
echo "----------------------------------------------------------------"
echo "Refresh https://monitor.easmoney.me/customers"
echo "The page now shows ONE row per call. Click a row to expand:"
echo "  - Captured info (key/value table)"
echo "  - Call meta (caller, channel, voice, duration, end reason)"
echo "  - Transcript (chat bubbles per turn)"
echo "  - Recording audio player (if flow has record_calls=true and a wav exists)"
echo "  - Collapsible tool calls + raw events"
echo "Live calls auto-stream new transcript turns into the expanded view."
echo "----------------------------------------------------------------"
echo "DONE."
