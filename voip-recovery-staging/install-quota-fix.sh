#!/bin/bash
# install-quota-fix.sh — fix HTTP 429 on AI generation by switching the default
# Gemini text model from gemini-2.5-pro (free tier limit:0) to gemini-2.5-flash
# (generous free tier). Also adds a frontend pre-check so the AI buttons are
# disabled with a clear banner whenever Gemini is unreachable / over quota /
# missing key — no more cryptic "Unexpected token '<'" failures.
#
# The voice agent (Gemini Live, gemini-3.1-flash-live-preview) is unaffected —
# it uses a different model + quota class. This fix touches only text gen.
#
# Optional override: set GEMINI_GEN_MODEL in /opt/sampath-ai/.env to pin a
# specific model (e.g. GEMINI_GEN_MODEL=gemini-2.5-pro once you upgrade billing).
#
# Idempotent. Backs up files as *.bak-quotafix-<ts>.
set -euo pipefail
if [ "$EUID" -ne 0 ]; then echo "must be root (use sudo)" >&2; exit 1; fi

STAGING=/home/horapusa/voip-recovery-staging
TS=$(date -u +%Y%m%dT%H%M%SZ)

for f in app.py flows/static/flows.js; do
  [ -f "$STAGING/$f" ] || { echo "missing: $STAGING/$f" >&2; exit 1; }
done
python3 -c "import ast; ast.parse(open('$STAGING/app.py').read())"
echo "==> app.py compiles"

for f in app.py:/opt/pbx-monitor/app.py \
         flows/static/flows.js:/opt/pbx-monitor/static/flows.js
do
  src="$STAGING/${f%%:*}"; dst="${f##*:}"
  if [ -f "$dst" ] && ! cmp -s "$src" "$dst"; then
    cp -a "$dst" "$dst.bak-quotafix-$TS"
    install -o asterisk -g asterisk -m 0644 "$src" "$dst"
    echo "==> ${f##*:} updated (backup: $(basename "$dst").bak-quotafix-$TS)"
  else
    echo "==> ${f##*:} unchanged"
  fi
done

systemctl restart pbx-monitor
sleep 1
systemctl is-active pbx-monitor && echo "==> pbx-monitor up"

echo
echo "----------------------------------------------------------------"
echo "Default text-generation model is now: gemini-2.5-flash"
echo "  (free tier quota available, fast, smart enough for prompts + flow diagrams)"
echo
echo "Verify by visiting in browser:"
echo "  https://monitor.easmoney.me/api/flows/_gemini-health"
echo "  Should show: \"model\": \"gemini-2.5-flash\", \"test_call_ok\": true"
echo
echo "Then reload /flows and try 'Generate prompts with AI' again — should work."
echo
echo "If you ever want to use gemini-2.5-pro (paid):"
echo "  1. Upgrade your Gemini API billing"
echo "  2. Add to /opt/sampath-ai/.env: GEMINI_GEN_MODEL=gemini-2.5-pro"
echo "  3. systemctl restart pbx-monitor"
echo "----------------------------------------------------------------"
echo "DONE."
