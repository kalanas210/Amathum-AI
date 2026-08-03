#!/bin/bash
# install-agents.sh — voice-agent + shared-data side for ALL live verticals
# (hospital + reservations). Supersedes install-hospital-agent.sh.
#
# Deploys:
#   /opt/sampath-ai/bridge.ts                  find_doctor/book_appointment/book_reservation handlers
#   /opt/sampath-ai/src/lib/gemini-live.ts     the booking tool declarations
#   /var/lib/sampath-ai/refdata/hospital.json      dummy doctor/branch/specialty catalog
#   /var/lib/sampath-ai/refdata/reservations.json  dummy area/slot/branch/deposit catalog
#   /var/lib/sampath-ai/bookings/{hospital/appointments,reservations/reservations,sales/{orders,leads}}/
# Flow wiring (via setup-flows.py):
#   - active hospital flow: APPOINTMENT line only — enables find_doctor +
#     book_appointment + guidance; lab is pulled off it if previously combined
#   - lab flow: created INACTIVE with order_lab_test + laboratory persona
#     (activate from the Flows page when the lab service goes live)
#   - reservations flow: created INACTIVE with book_reservation + restaurant persona
#     (activate it from the Flows page to take reservation calls — one vertical at a time)
#
# Idempotent. Backs up replaced files as *.bak-agents-<ts>. Restarts sampath-ai.
# Pair with: sudo bash install-dashboards.sh  (Flask API + dashboard UI).
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
  "$STAGING/setup-flows.py"
  "$DASH/refdata/hospital.json"
  "$DASH/refdata/reservations.json"
  "$DASH/refdata/sales.json"
)
for f in "${need[@]}"; do [ -f "$f" ] || { echo "missing: $f" >&2; exit 1; }; done

# --- preflight: JSON + python + TS transform ---
for j in hospital reservations sales; do
  python3 -c "import json; json.load(open('$DASH/refdata/$j.json'))"
done
python3 -c "import ast; ast.parse(open('$STAGING/setup-flows.py').read())"
echo "==> refdata JSON + setup-flows.py parse OK"
if [ -x "$ESB" ]; then
  for f in "$STAGING/bridge.ts" "$STAGING/flows/patches/gemini-live.ts"; do
    "$ESB" "$f" --bundle=false --platform=node --format=esm --loader:.ts=ts >/dev/null
  done
  echo "==> bridge.ts + gemini-live.ts pass esbuild transform"
else
  echo "WARN: esbuild missing at $ESB — skipping TS check" >&2
fi

backup_install() { # src dst
  local src="$1" dst="$2"
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then echo "==> $(basename "$dst") unchanged"; return; fi
  [ -f "$dst" ] && cp -a "$dst" "$dst.bak-agents-$TS"
  install -o asterisk -g asterisk -m 0644 "$src" "$dst"
  echo "==> $(basename "$dst") deployed$([ -f "$dst.bak-agents-$TS" ] && echo " (backup .bak-agents-$TS)")"
}

# --- data dirs ---
install -d -o asterisk -g asterisk -m 0755 \
  "$DATA/refdata" "$DATA/bookings" \
  "$DATA/bookings/hospital" "$DATA/bookings/hospital/appointments" "$DATA/bookings/hospital/labs" \
  "$DATA/bookings/reservations" "$DATA/bookings/reservations/reservations" \
  "$DATA/bookings/sales" "$DATA/bookings/sales/orders" "$DATA/bookings/sales/leads"

# --- deploy code + reference data ---
backup_install "$STAGING/bridge.ts" "$SAI/bridge.ts"
backup_install "$STAGING/flows/patches/gemini-live.ts" "$SAI/src/lib/gemini-live.ts"
backup_install "$DASH/refdata/hospital.json" "$DATA/refdata/hospital.json"
backup_install "$DASH/refdata/reservations.json" "$DATA/refdata/reservations.json"
backup_install "$DASH/refdata/sales.json" "$DATA/refdata/sales.json"

# --- wire booking tools/guidance into the flows (backup first) ---
[ -d "$DATA/flows" ] && cp -a "$DATA/flows" "$DATA/flows.bak-agents-$TS" 2>/dev/null || true
python3 "$STAGING/setup-flows.py" "$DATA"
chown -R asterisk:asterisk "$DATA/flows" "$DATA/refdata" "$DATA/bookings"

# --- restart the voice agent (tool-registry change needs a restart) ---
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
Voice-agent booking installed. Appointment and Lab are now SEPARATE flows.
 - Hospital (active flow): APPOINTMENT line — agent can find_doctor +
   book_appointment on a call. Lab tools/guidance are no longer on this line.
 - Lab: a flow was created but is INACTIVE. Activate it when the lab service
   goes live (Flows page -> Lab Services Agent -> Activate, OR set active_id to
   "lab" in /var/lib/sampath-ai/active-flow.json). One vertical at a time:
   activating lab takes the appointment agent off the line, so run it on its own
   number/line.
 - Reservations + Sales: also created INACTIVE (activate from the Flows page).
 - Bookings land in /var/lib/sampath-ai/bookings/<vertical>/... and show on the
   matching dashboard within ~4s.

Also deploy the dashboard + API side if you haven't:
   sudo bash /home/horapusa/voip-recovery-staging/install-dashboards.sh

Watch a test call live:  journalctl -u sampath-ai -f
----------------------------------------------------------------
DONE.
EOF
