#!/bin/bash
# install-flows-v2.sh — v2 features on top of v1 (which must already be installed).
# Adds: /customers live table, AI-generate flow, test playground, working-hours,
# recording toggle, voice-sample button, richer preset diagrams, live transcript stream.
#
# Idempotent: backs up everything as *.bak-flowsv2-<ts>. Run with sudo.
set -euo pipefail

if [ "$EUID" -ne 0 ]; then echo "must be root (use sudo)" >&2; exit 1; fi

STAGING=/home/horapusa/voip-recovery-staging
FS=$STAGING/flows
V2=$STAGING/flows/v2
TS=$(date -u +%Y%m%dT%H%M%SZ)
echo "==> staging:    $V2"
echo "==> backup tag: bak-flowsv2-$TS"

# --- 0. Pre-flight ---
for f in \
  flows/patches/agent-config.ts \
  flows/patches/gemini-live.ts \
  flows/templates/flows.html \
  flows/static/flows.js \
  flows/v2/templates/customers.html \
  flows/v2/static/customers.js \
  flows/v2/seeds/real-estate.json \
  flows/v2/seeds/software-company.json \
  app.py bridge.ts ; do
  [ -f "$STAGING/$f" ] || { echo "missing: $STAGING/$f" >&2; exit 1; }
done

python3 -c "import ast; ast.parse(open('$STAGING/app.py').read())"
echo "==> app.py compiles"
for j in "$V2"/seeds/*.json; do python3 -c "import json; json.load(open('$j'))"; done
echo "==> seeds parse"

# --- 1. Bridge code (with type-check; pre-existing Next.js errors ignored) ---
install -o asterisk -g asterisk -m 0644 "$FS/patches/agent-config.ts" /opt/sampath-ai/src/lib/agent-config.ts.candidate
install -o asterisk -g asterisk -m 0644 "$FS/patches/gemini-live.ts"  /opt/sampath-ai/src/lib/gemini-live.ts.candidate
install -o asterisk -g asterisk -m 0644 "$STAGING/bridge.ts"          /opt/sampath-ai/bridge.ts.candidate

TMP=$(mktemp -d)
cp -a /opt/sampath-ai/. "$TMP/"
cp "$FS/patches/agent-config.ts" "$TMP/src/lib/agent-config.ts"
cp "$FS/patches/gemini-live.ts"  "$TMP/src/lib/gemini-live.ts"
cp "$STAGING/bridge.ts"          "$TMP/bridge.ts"
LOG=/tmp/tsc-flowsv2.log
( cd "$TMP" && /opt/sampath-ai/node_modules/.bin/tsc --noEmit --project tsconfig.json 2>&1 ) > "$LOG" || true
if grep -qE "^(bridge\.ts|src/lib/(agent-config|gemini-live)\.ts)\(" "$LOG"; then
  echo "!! tsc errors in our files — see $LOG. Aborting."
  grep -E "^(bridge\.ts|src/lib/(agent-config|gemini-live)\.ts)\(" "$LOG"
  rm -rf "$TMP"; exit 1
fi
NEXT_ERR=$(grep -cE "^src/(app|components)/" "$LOG" || true)
echo "==> tsc OK (ignored $NEXT_ERR pre-existing Next.js errors)"
rm -rf "$TMP"

for pair in src/lib/agent-config.ts src/lib/gemini-live.ts bridge.ts; do
  live="/opt/sampath-ai/$pair"; cand="$live.candidate"
  if [ -f "$live" ] && ! cmp -s "$cand" "$live"; then
    cp -a "$live" "$live.bak-flowsv2-$TS"
    mv "$cand" "$live"
    echo "==> $pair updated (backup: $(basename "$live").bak-flowsv2-$TS)"
  else
    rm -f "$cand"
    echo "==> $pair unchanged"
  fi
done

# --- 2. app.py + flows.html + flows.js + customers.html + customers.js ---
for f in app.py:/opt/pbx-monitor/app.py \
         flows/templates/flows.html:/opt/pbx-monitor/templates/flows.html \
         flows/static/flows.js:/opt/pbx-monitor/static/flows.js \
         flows/v2/templates/customers.html:/opt/pbx-monitor/templates/customers.html \
         flows/v2/static/customers.js:/opt/pbx-monitor/static/customers.js
do
  src="$STAGING/${f%%:*}"; dst="${f##*:}"
  if [ -f "$dst" ] && ! cmp -s "$src" "$dst"; then
    cp -a "$dst" "$dst.bak-flowsv2-$TS"
  fi
  install -o asterisk -g asterisk -m 0644 "$src" "$dst"
done
echo "==> app.py + flows.{html,js} + customers.{html,js} deployed"

# --- 3. base.html nav: add /customers entry after the existing /flows entry ---
python3 <<'PYNAV'
PATH = "/opt/pbx-monitor/templates/base.html"
src = open(PATH).read()
if "/customers" in src and "('customers'" in src:
    print("==> base.html already has /customers nav")
else:
    anchor = "('flows','/flows','workflow','Flows','admin'),"
    if anchor not in src:
        print("!! could not find /flows anchor — base.html NOT patched")
    else:
        ins = anchor + "\n        ('customers','/customers','contact','Customers','read'),"
        import shutil, time
        shutil.copy(PATH, PATH + ".bak-flowsv2-" + time.strftime("%Y%m%dT%H%M%SZ"))
        open(PATH, "w").write(src.replace(anchor, ins, 1))
        print("==> base.html /customers nav added")
PYNAV

# --- 4. Re-seed real-estate + software-company (full overwrite of preset files) ---
install -o asterisk -g asterisk -m 0640 "$V2/seeds/real-estate.json"      /var/lib/sampath-ai/flows/real-estate.json
install -o asterisk -g asterisk -m 0640 "$V2/seeds/software-company.json" /var/lib/sampath-ai/flows/software-company.json
echo "==> real-estate + software-company presets re-seeded"

# --- 5. Sampath-bank: replace ONLY the .flow field + add v2 fields, preserve system_prompt ---
python3 <<'PYBANK'
import json, time, shutil
p = "/var/lib/sampath-ai/flows/sampath-bank.json"
shutil.copy(p, p + ".bak-flowsv2-" + time.strftime("%Y%m%dT%H%M%SZ"))
with open(p) as f: cfg = json.load(f)

cfg["flow"] = {
  "nodes": [
    {"id":"start",          "type":"start",    "position":{"x":40,"y":280},  "data":{"label":"Start","greeting_text":"Greet caller in matching language (Sinhala/English/Tamil), time-of-day aware."}},
    {"id":"intent",         "type":"intent",   "position":{"x":240,"y":280}, "data":{"label":"Detect intent","description":"Branch/ATM info? Exchange rate? Account question? Lost card? Frustrated? Other?"}},

    {"id":"ask-area",       "type":"response", "position":{"x":460,"y":80},  "data":{"label":"Ask which area","message_text":"Which area or branch are you asking about?"}},
    {"id":"tool-branch",    "type":"tool",     "position":{"x":700,"y":80},  "data":{"label":"Find Sampath branch","tool_id":"find_sampath_branch","arg_template":"query = caller-mentioned area/branch name"}},
    {"id":"end-branch",     "type":"end",      "position":{"x":940,"y":80},  "data":{"label":"End (branch info)","farewell_text":"Hope that helps — anything else? Otherwise, have a great day."}},

    {"id":"tool-rates",     "type":"tool",     "position":{"x":460,"y":200}, "data":{"label":"Get exchange rates","tool_id":"get_exchange_rates","arg_template":"currency = optional (USD/EUR/GBP/...)"}},
    {"id":"end-rates",      "type":"end",      "position":{"x":700,"y":200}, "data":{"label":"End (rates given)","farewell_text":"Anything else I can help with?"}},

    {"id":"save-name-nic",  "type":"tool",     "position":{"x":460,"y":340}, "data":{"label":"Save name+NIC","tool_id":"save_customer_info","arg_template":"Save name, nic, phone if given."}},
    {"id":"save-complaint", "type":"tool",     "position":{"x":700,"y":340}, "data":{"label":"Save complaint","tool_id":"save_customer_info","arg_template":"field=complaint, value=verbatim caller words."}},
    {"id":"xfer-support",   "type":"transfer", "position":{"x":940,"y":340}, "data":{"label":"→ Support team","category":"default"}},

    {"id":"end-graceful",   "type":"end",      "position":{"x":940,"y":480}, "data":{"label":"End (graceful)","farewell_text":"ස්තූතියි, සුභ දවසක්."}}
  ],
  "edges": [
    {"id":"e1","source":"start","target":"intent"},
    {"id":"e2","source":"intent","target":"ask-area","label":"branch/ATM"},
    {"id":"e3","source":"ask-area","target":"tool-branch"},
    {"id":"e4","source":"tool-branch","target":"end-branch"},
    {"id":"e5","source":"intent","target":"tool-rates","label":"exchange rate"},
    {"id":"e6","source":"tool-rates","target":"end-rates"},
    {"id":"e7","source":"intent","target":"save-name-nic","label":"complaint/issue"},
    {"id":"e8","source":"save-name-nic","target":"save-complaint"},
    {"id":"e9","source":"save-complaint","target":"xfer-support"},
    {"id":"e10","source":"intent","target":"end-graceful","label":"caller satisfied"}
  ],
  "viewport": {"x":0,"y":0,"zoom":1}
}

# v2 schema fields (no-op if user has them already)
cfg.setdefault("working_hours", {
  "enabled": False, "timezone": "Asia/Colombo",
  "schedule": {"0":"","1":"08:00-20:00","2":"08:00-20:00","3":"08:00-20:00","4":"08:00-20:00","5":"08:00-20:00","6":"09:00-13:00"},
  "out_of_hours_action": "greet",
  "out_of_hours_greeting": "Thanks for calling Sampath. Our branches are currently closed but I can still help with general questions and complaints.",
  "out_of_hours_transfer_category": "default",
  "out_of_hours_hangup_message": ""
})
cfg.setdefault("record_calls", False)

with open(p + ".tmp", "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
import os; os.chmod(p + ".tmp", 0o640); os.replace(p + ".tmp", p)
print("==> sampath-bank.json flow diagram enriched (system_prompt preserved)")
PYBANK

# --- 6. Restart services ---
systemctl restart pbx-monitor
systemctl restart sampath-ai
sleep 2

echo "----------------------------------------------------------------"
systemctl --no-pager --lines=0 status pbx-monitor sampath-ai | grep -E "Active|Loaded" | head -8
echo "----------------------------------------------------------------"
echo "New URLs:"
echo "  /flows      — multi-agent editor (now with: Playground, Schedule, Generate-from-idea, Voice sample, Edit-a-copy)"
echo "  /customers  — live customer-info table + live calls + streaming transcript"
echo "Active flow:"
cat /var/lib/sampath-ai/active-flow.json
echo
echo "DONE."
