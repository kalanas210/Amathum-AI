#!/bin/bash
# install-flows.sh — Multi-agent + visual flow builder for the Sampath AI bridge.
# Bundles: data model migration, bridge code changes, dialplan patch, admin /flows page.
# Idempotent: backs up originals as *.bak-flows-<ts> before overwriting.
# Run with sudo: sudo bash /home/horapusa/voip-recovery-staging/install-flows.sh
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "must be run as root (use sudo)" >&2
  exit 1
fi

STAGING=/home/horapusa/voip-recovery-staging
FSTAGING=$STAGING/flows
TS=$(date -u +%Y%m%dT%H%M%SZ)
echo "==> staging:    $FSTAGING"
echo "==> backup tag: bak-flows-$TS"

# --- 0. Pre-flight: validate everything we are about to deploy ---
for f in \
  flows/migrate-flows.py \
  flows/seeds/real-estate.json \
  flows/seeds/software-company.json \
  flows/patches/agent-config.ts \
  flows/patches/gemini-live.ts \
  flows/templates/flows.html \
  flows/static/flows.js \
  app.py \
  bridge.ts ; do
  [ -f "$STAGING/$f" ] || { echo "missing: $STAGING/$f" >&2; exit 1; }
done

python3 -m py_compile "$STAGING/app.py"
echo "==> app.py compiles"

# Validate seed JSONs parse
for j in "$FSTAGING"/seeds/*.json; do
  python3 -c "import json,sys; json.load(open('$j'))"
done
echo "==> seed JSONs valid"

# --- 1. /var/lib/sampath-ai/flows directory ---
install -d -o asterisk -g asterisk -m 0750 /var/lib/sampath-ai/flows
echo "==> /var/lib/sampath-ai/flows ready"

# --- 2. Install + run migration ---
install -o root -g root -m 0755 "$FSTAGING/migrate-flows.py" /opt/pbx-monitor/migrate-flows.py
install -o asterisk -g asterisk -m 0640 "$FSTAGING/seeds/real-estate.json"      /opt/pbx-monitor/flow-seeds-real-estate.json
install -o asterisk -g asterisk -m 0640 "$FSTAGING/seeds/software-company.json" /opt/pbx-monitor/flow-seeds-software-company.json
# Migrate script reads seeds from a sibling 'seeds/' dir. Provide one alongside it.
install -d -o root -g root -m 0755 /opt/pbx-monitor/seeds
cp -a "$FSTAGING/seeds/." /opt/pbx-monitor/seeds/
chown -R asterisk:asterisk /opt/pbx-monitor/seeds
ln -snf /opt/pbx-monitor/seeds /opt/pbx-monitor/seeds  # noop; left for clarity
# Run the migration as asterisk so the files are owned correctly.
sudo -u asterisk SEEDS_DIR=/opt/pbx-monitor/seeds python3 /opt/pbx-monitor/migrate-flows.py || true
# Re-run normalised so the script's relative path discovery just works:
( cd /opt/pbx-monitor && sudo -u asterisk python3 migrate-flows.py )
echo "==> migration applied"
ls -la /var/lib/sampath-ai/flows/

# --- 3. Stage bridge code changes to /opt/sampath-ai/.candidate files ---
install -o asterisk -g asterisk -m 0644 "$FSTAGING/patches/agent-config.ts" /opt/sampath-ai/src/lib/agent-config.ts.candidate
install -o asterisk -g asterisk -m 0644 "$FSTAGING/patches/gemini-live.ts"  /opt/sampath-ai/src/lib/gemini-live.ts.candidate
install -o asterisk -g asterisk -m 0644 "$STAGING/bridge.ts"                /opt/sampath-ai/bridge.ts.candidate

# Type-check together with the rest of the project. The /opt/sampath-ai project
# also contains a Next.js admin frontend (src/app/, src/components/) whose tsc
# errors PRE-DATE this change — they fail without a Next.js build environment.
# We only fail the install on errors in OUR files (bridge.ts / lib/agent-config.ts /
# lib/gemini-live.ts).
TMP_TS=$(mktemp -d)
cp -a /opt/sampath-ai/. "$TMP_TS/"
cp "$FSTAGING/patches/agent-config.ts" "$TMP_TS/src/lib/agent-config.ts"
cp "$FSTAGING/patches/gemini-live.ts"  "$TMP_TS/src/lib/gemini-live.ts"
cp "$STAGING/bridge.ts"                "$TMP_TS/bridge.ts"
TSC_LOG=/tmp/tsc-flows.log
( cd "$TMP_TS" && /opt/sampath-ai/node_modules/.bin/tsc --noEmit --project tsconfig.json 2>&1 ) > "$TSC_LOG" || true
if grep -qE "^(bridge\.ts|src/lib/(agent-config|gemini-live)\.ts)\(" "$TSC_LOG"; then
  echo "!! tsc errors in our files — see $TSC_LOG. NOT touching live files."
  grep -E "^(bridge\.ts|src/lib/(agent-config|gemini-live)\.ts)\(" "$TSC_LOG"
  rm -rf "$TMP_TS"
  exit 1
fi
NEXT_ERR_COUNT=$(grep -cE "^src/(app|components)/" "$TSC_LOG" || true)
echo "==> tsc OK for our 3 files (ignored $NEXT_ERR_COUNT pre-existing Next.js errors in src/app and src/components)"
rm -rf "$TMP_TS"

# Atomic swap with backup
for pair in \
  "src/lib/agent-config.ts" \
  "src/lib/gemini-live.ts" \
  "bridge.ts"
do
  live="/opt/sampath-ai/$pair"
  cand="$live.candidate"
  if [ -f "$live" ] && ! cmp -s "$cand" "$live"; then
    cp -a "$live" "$live.bak-flows-$TS"
    mv "$cand" "$live"
    echo "==> $pair replaced (backup: $(basename "$live").bak-flows-$TS)"
  else
    rm -f "$cand"
    echo "==> $pair unchanged or no live file"
  fi
done

# --- 4. /etc/asterisk/extensions.conf [ai-escalate] patch (idempotent) ---
python3 <<'PYDIAL'
import io
PATH = "/etc/asterisk/extensions.conf"
MARK = "; FLOWS-PATCH-V1"
src = open(PATH).read()
if MARK in src:
    print("==> extensions.conf already patched")
else:
    needle = " same => n,Dial(PJSIP/${MGR}@pabx"
    if needle not in src:
        print("!! could not find Dial(PJSIP/${MGR}@pabx anchor — extensions.conf NOT patched")
    else:
        ins = (
            ' same => n,ExecIf($["${AI_MGR_NUMBER}" != "" & "${TESTMODE}" = "false"]?Set(MGR=${AI_MGR_NUMBER})) ' + MARK + '\n'
            ' same => n,NoOp(Final MGR=${MGR} AI_MGR_NUMBER=${AI_MGR_NUMBER}) ' + MARK + '\n'
        )
        new = src.replace(needle, ins + needle, 1)
        import shutil, time
        shutil.copy(PATH, PATH + ".bak-flows-" + time.strftime("%Y%m%dT%H%M%SZ"))
        open(PATH, "w").write(new)
        print("==> extensions.conf patched")
PYDIAL

# --- 5. /opt/pbx-monitor/app.py (replace with staged) ---
if ! cmp -s "$STAGING/app.py" /opt/pbx-monitor/app.py; then
  cp -a /opt/pbx-monitor/app.py "/opt/pbx-monitor/app.py.bak-flows-$TS"
  install -o asterisk -g asterisk -m 0644 "$STAGING/app.py" /opt/pbx-monitor/app.py
  echo "==> app.py replaced"
else
  echo "==> app.py unchanged"
fi

# --- 6. Template + static asset ---
install -o asterisk -g asterisk -m 0644 "$FSTAGING/templates/flows.html" /opt/pbx-monitor/templates/flows.html
install -o asterisk -g asterisk -m 0644 "$FSTAGING/static/flows.js"      /opt/pbx-monitor/static/flows.js
echo "==> flows.html + flows.js installed"

# --- 7. base.html nav entry (idempotent insert after the ai-agent line) ---
python3 <<'PYNAV'
PATH = "/opt/pbx-monitor/templates/base.html"
MARK = "/flows"
src = open(PATH).read()
if "('flows','/flows'" in src or "('flows', '/flows'" in src:
    print("==> base.html already has flows nav entry")
else:
    anchor = "('ai-agent','/ai-agent','bot','AI Agent','config'),"
    if anchor not in src:
        print("!! could not find AI Agent nav anchor — base.html NOT patched")
    else:
        ins = anchor + "\n        ('flows','/flows','workflow','Flows','admin'),"
        new = src.replace(anchor, ins, 1)
        import shutil, time
        shutil.copy(PATH, PATH + ".bak-flows-" + time.strftime("%Y%m%dT%H%M%SZ"))
        open(PATH, "w").write(new)
        print("==> base.html nav patched")
PYNAV

# --- 8. Reload asterisk dialplan + restart pbx-monitor + sampath-ai ---
asterisk -rx "dialplan reload" >/dev/null 2>&1 && echo "==> asterisk dialplan reloaded" || echo "!! dialplan reload failed (try manually)"

systemctl restart pbx-monitor
systemctl restart sampath-ai
sleep 2

echo "----------------------------------------------------------------"
echo "Status:"
systemctl --no-pager --lines=0 status pbx-monitor sampath-ai | grep -E "Active|Loaded" | head -8
echo "----------------------------------------------------------------"
echo "Flows on disk:"
ls -la /var/lib/sampath-ai/flows/
echo "Active flow:"
cat /var/lib/sampath-ai/active-flow.json 2>/dev/null || echo "(no active-flow.json yet)"
echo "----------------------------------------------------------------"
echo "Now open https://monitor.easmoney.me/flows  (admin role required)."
echo "Default active flow: sampath-bank. Two presets ship alongside:"
echo "  - real-estate            (Real Estate Booking Agent)"
echo "  - software-company       (Software Company Agent)"
echo "----------------------------------------------------------------"
echo "DONE."
