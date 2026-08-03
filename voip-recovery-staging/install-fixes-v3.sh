#!/bin/bash
# install-fixes-v3.sh — fixes for the issues observed after v2:
#   1) /var/lib/sampath-ai/flows/sampath-bank.json got root-owned by v2's patcher
#      (mode 0640) — pbx-monitor (asterisk) can't read it, so the panel showed
#      "voice: ?" and "undefined rule(s)". Fixed by chown back to asterisk.
#   2) AI prompt generation INSIDE the editor (new "Generate prompts with AI"
#      button on the Prompts tab). New endpoint /api/flows/<id>/regenerate-prompts.
#   3) Flow diagram canvas hardening: stats line, fit-view button, empty-state
#      with one-click "Generate with AI" using new /api/flows/<id>/regenerate-flow.
# Idempotent. Backs up files as *.bak-fixv3-<ts>.
set -euo pipefail
if [ "$EUID" -ne 0 ]; then echo "must be root (use sudo)" >&2; exit 1; fi

STAGING=/home/horapusa/voip-recovery-staging
TS=$(date -u +%Y%m%dT%H%M%SZ)

for f in app.py flows/templates/flows.html flows/static/flows.js; do
  [ -f "$STAGING/$f" ] || { echo "missing: $STAGING/$f" >&2; exit 1; }
done
python3 -c "import ast; ast.parse(open('$STAGING/app.py').read())"
echo "==> app.py compiles"

# --- 1. Repair ownership + permissions on the flows store ---
chown -R asterisk:asterisk /var/lib/sampath-ai/flows /var/lib/sampath-ai/active-flow.json 2>/dev/null || true
chmod 0750 /var/lib/sampath-ai/flows
chmod 0640 /var/lib/sampath-ai/flows/*.json 2>/dev/null || true
chmod 0644 /var/lib/sampath-ai/active-flow.json 2>/dev/null || true
echo "==> flows store ownership/perms repaired"
ls -la /var/lib/sampath-ai/flows/ | head -10

# --- 2. Deploy the three changed files (with backups) ---
for f in app.py:/opt/pbx-monitor/app.py \
         flows/templates/flows.html:/opt/pbx-monitor/templates/flows.html \
         flows/static/flows.js:/opt/pbx-monitor/static/flows.js
do
  src="$STAGING/${f%%:*}"; dst="${f##*:}"
  if [ -f "$dst" ] && ! cmp -s "$src" "$dst"; then
    cp -a "$dst" "$dst.bak-fixv3-$TS"
    install -o asterisk -g asterisk -m 0644 "$src" "$dst"
    echo "==> ${f##*:} updated (backup: $(basename "$dst").bak-fixv3-$TS)"
  else
    echo "==> ${f##*:} unchanged"
  fi
done

systemctl restart pbx-monitor
sleep 1
systemctl is-active pbx-monitor && echo "==> pbx-monitor up"

echo
echo "----------------------------------------------------------------"
echo "Refresh https://monitor.easmoney.me/flows"
echo "  - Sampath Bank card should now show 'voice: Aoede · 1 rule(s) · 5 tool(s)' (no more undefined)"
echo "  - Open ANY flow → Prompts tab → click 'Generate prompts with AI'"
echo "    Describe your use case, Gemini drafts all 4 prompts, Apply puts them in the editor (not saved until you click Save)"
echo "  - Flow diagram tab now shows node/edge count, has a Fit view button, and an empty-state"
echo "    with a 'Generate with AI' button if the flow has 0 nodes."
echo "  - Inside the diagram modal you can add free-text guidance for the AI."
echo "----------------------------------------------------------------"
echo "DONE."
