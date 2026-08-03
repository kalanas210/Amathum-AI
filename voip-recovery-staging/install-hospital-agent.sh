#!/bin/bash
# install-hospital-agent.sh — give the Naxter voice agent (sampath-ai bridge) the
# ability to BOOK APPOINTMENTS on a live call, write them to a real server-side
# store, and surface them on the Hospital dashboard in real time.
#
# Deploys:
#   /opt/sampath-ai/bridge.ts                  find_doctor + book_appointment handlers + booking writer
#   /opt/sampath-ai/src/lib/gemini-live.ts     the two tool declarations
#   /var/lib/sampath-ai/refdata/hospital.json  dummy doctor / branch / specialty catalog
#   /var/lib/sampath-ai/bookings/hospital/appointments/   (created — real bookings land here)
# Updates the active hospital flow to enable find_doctor + book_appointment and
# append booking guidance to its instructions (Sinhala persona preserved).
#
# Idempotent. Backs up every replaced file as *.bak-hospital-<ts>. Restarts sampath-ai.
# Pair with: sudo bash install-dashboards.sh  (deploys the Flask API + dashboard UI).
set -euo pipefail
if [ "$EUID" -ne 0 ]; then echo "must be root (use sudo)" >&2; exit 1; fi

STAGING=/home/horapusa/voip-recovery-staging
DASH=$STAGING/dashboards
SAI=/opt/sampath-ai
DATA=/var/lib/sampath-ai
TS=$(date -u +%Y%m%dT%H%M%SZ)
ESB=$SAI/node_modules/.bin/esbuild

need=(
  "$STAGING/bridge.ts"
  "$STAGING/flows/patches/gemini-live.ts"
  "$DASH/refdata/hospital.json"
)
for f in "${need[@]}"; do [ -f "$f" ] || { echo "missing: $f" >&2; exit 1; }; done

# --- preflight: JSON + TS transform checks (what tsx must accept at startup) ---
python3 -c "import json; json.load(open('$DASH/refdata/hospital.json'))"
echo "==> refdata/hospital.json is valid JSON"
if [ -x "$ESB" ]; then
  for f in "$STAGING/bridge.ts" "$STAGING/flows/patches/gemini-live.ts"; do
    "$ESB" "$f" --bundle=false --platform=node --format=esm --loader:.ts=ts >/dev/null
  done
  echo "==> bridge.ts + gemini-live.ts pass esbuild transform"
else
  echo "WARN: esbuild not found at $ESB — skipping TS transform check" >&2
fi

backup_install() { # src dst
  local src="$1" dst="$2"
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then echo "==> $(basename "$dst") unchanged"; return; fi
  [ -f "$dst" ] && cp -a "$dst" "$dst.bak-hospital-$TS"
  install -o asterisk -g asterisk -m 0644 "$src" "$dst"
  echo "==> $(basename "$dst") deployed$([ -f "$dst.bak-hospital-$TS" ] && echo " (backup .bak-hospital-$TS)")"
}

# --- data dirs (owned by asterisk = the bridge + flask user) ---
install -d -o asterisk -g asterisk -m 0755 \
  "$DATA/refdata" "$DATA/bookings" "$DATA/bookings/hospital" "$DATA/bookings/hospital/appointments"

# --- deploy code + reference data ---
backup_install "$STAGING/bridge.ts" "$SAI/bridge.ts"
backup_install "$STAGING/flows/patches/gemini-live.ts" "$SAI/src/lib/gemini-live.ts"
backup_install "$DASH/refdata/hospital.json" "$DATA/refdata/hospital.json"

# --- enable booking tools + guidance on the active hospital flow ---
FLOW_ID="${1:-$(python3 -c "import json;print(json.load(open('$DATA/active-flow.json')).get('active_id',''))" 2>/dev/null || true)}"
FLOW_FILE="$DATA/flows/$FLOW_ID.json"
if [ -n "${FLOW_ID:-}" ] && [ -f "$FLOW_FILE" ]; then
  cp -a "$FLOW_FILE" "$FLOW_FILE.bak-hospital-$TS"
  python3 - "$FLOW_FILE" <<'PYEOF'
import json, os, sys
fp = sys.argv[1]
cfg = json.load(open(fp))
tools = cfg.get('tools_enabled') or []
for t in ['save_customer_info', 'find_doctor', 'book_appointment', 'request_human_transfer', 'end_call']:
    if t not in tools:
        tools.append(t)
cfg['tools_enabled'] = tools
MARK = 'NAXTER_BOOKING_BLOCK_V1'
ci = cfg.get('custom_instructions') or ''
if MARK not in ci:
    ci += (
        "\n\n### " + MARK + " ###\n"
        "## APPOINTMENT BOOKING\n"
        "You can book doctor appointments for callers. When a caller wants to see a doctor or describes a symptom:\n"
        "1. Clarify the problem and ask which branch/city they prefer if not stated.\n"
        "2. Call find_doctor (pass the symptom, and branch if given) to get available doctors. Offer one or two by name, branch, fee and a couple of times. Never invent a doctor.\n"
        "3. Collect the patient's full name and a contact phone number.\n"
        "4. Resolve relative dates ('tomorrow', 'next Monday') to an absolute YYYY-MM-DD using the current Sri Lanka date in your context.\n"
        "5. Read the full booking back (patient, doctor, branch, date, time) and ask the caller to confirm.\n"
        "6. ONLY after they confirm, call book_appointment with all the details. Then tell them it is confirmed and read out the confirmation reference and the consultation fee.\n"
        "If find_doctor returns nothing, offer a General Medicine doctor or ask them to clarify. Never invent a fee, doctor, branch or reference number — use only what the tools return.\n"
    )
    cfg['custom_instructions'] = ci
tmp = fp + '.tmp'
json.dump(cfg, open(tmp, 'w'), indent=2, ensure_ascii=False)
os.replace(tmp, fp)
print('   tools_enabled:', ', '.join(cfg['tools_enabled']))
PYEOF
  chown asterisk:asterisk "$FLOW_FILE"
  echo "==> flow '$FLOW_ID' updated (booking tools + guidance; backup .bak-hospital-$TS)"
else
  echo "WARN: flow file for '${FLOW_ID:-?}' not found — enable find_doctor + book_appointment on your hospital flow manually (Flows page or its JSON)." >&2
fi

# --- restart the voice agent (tool registry change needs a restart) ---
systemctl restart sampath-ai
sleep 2
if systemctl is-active --quiet sampath-ai; then
  echo "==> sampath-ai is up"
else
  echo "ERROR: sampath-ai did not come back up — recent logs:" >&2
  journalctl -u sampath-ai -n 25 --no-pager >&2 || true
  exit 1
fi

cat <<'EOF'

----------------------------------------------------------------
Hospital voice-agent booking installed.
 - On a live call the agent can now find a doctor and book an appointment.
 - Bookings are written to /var/lib/sampath-ai/bookings/hospital/appointments/
   and appear on the Hospital dashboard (auto-refreshing) within ~4s.
 - Edit the doctor/branch catalog any time: /var/lib/sampath-ai/refdata/hospital.json

If you have NOT already, also deploy the dashboard + API side:
   sudo bash /home/horapusa/voip-recovery-staging/install-dashboards.sh

Then place a test call and ask to book a doctor. Watch it happen live:
   journalctl -u sampath-ai -f
----------------------------------------------------------------
DONE.
EOF
