#!/bin/bash
# install-outbound.sh — add the [ai-outbound] dialplan context for Phase 2 outbound
# AI confirmation calls, ADDITIVELY (the inbound [ai-agent] contexts are untouched),
# and reload the dialplan. Idempotent.
#
# Prereqs (run these too):
#   sudo bash install-agents.sh       # bridge with the 9092 outbound listener + confirm_order tool
#   sudo bash install-dashboards.sh   # the /confirm-call endpoint + dashboard button
set -euo pipefail
if [ "$EUID" -ne 0 ]; then echo "must be root (use sudo)" >&2; exit 1; fi

STAGING=/home/horapusa/voip-recovery-staging
SNIP=$STAGING/asterisk/ai-outbound.conf
EXT=/etc/asterisk/extensions.conf
TS=$(date -u +%Y%m%dT%H%M%SZ)
[ -f "$SNIP" ] || { echo "missing $SNIP" >&2; exit 1; }

install -d -o asterisk -g asterisk -m 0755 /var/lib/sampath-ai/outbound

if grep -q '^\[ai-outbound\]' "$EXT"; then
  echo "==> [ai-outbound] already present in $EXT — leaving as-is"
else
  cp -a "$EXT" "$EXT.bak-outbound-$TS"
  printf '\n' >> "$EXT"
  cat "$SNIP" >> "$EXT"
  echo "==> appended [ai-outbound] to $EXT (backup: $EXT.bak-outbound-$TS)"
fi

asterisk -rx "dialplan reload" >/dev/null
sleep 1
if asterisk -rx "dialplan show ai-outbound" 2>/dev/null | grep -qi "ai-outbound"; then
  echo "==> [ai-outbound] is loaded in Asterisk"
else
  echo "ERROR: [ai-outbound] did not load. Check: asterisk -rx 'dialplan show ai-outbound'" >&2
  echo "       (existing dialplan keeps running; nothing inbound was changed.)" >&2
  exit 1
fi

cat <<'EOF'

----------------------------------------------------------------
Outbound AI confirmation calls are wired.

>>> SAFETY FIRST <<<
Before the first real call, set a test number in
  /var/lib/sampath-ai/refdata/sales.json  ->  "confirm_test_number": "07XXXXXXXX"
so EVERY confirm-call rings YOUR phone, not real customers. Clear it ("") to go live.

Test: Sales dashboard -> open a pending order -> "Call to confirm (live AI call)".
The agent calls, reads the order back, and on confirmation flips the order to
Confirmed (or Cancelled) on the dashboard. Watch it:  journalctl -u sampath-ai -f
----------------------------------------------------------------
DONE.
EOF
