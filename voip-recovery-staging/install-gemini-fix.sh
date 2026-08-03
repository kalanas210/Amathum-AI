#!/bin/bash
# install-gemini-fix.sh — fixes the "Unexpected token '<'" error in AI generation.
# Root cause: my endpoint returned HTTP 502 on Gemini failures; Cloudflare intercepts
# 5xx responses and replaces the body with its own HTML page, which breaks JSON parsing
# in the browser. Fix: return HTTP 200 with {ok:false, error} for downstream API
# failures so the real error message reaches the UI.
#
# Also adds GET /api/flows/_gemini-health for diagnosing key/network/quota issues.
set -euo pipefail
if [ "$EUID" -ne 0 ]; then echo "must be root (use sudo)" >&2; exit 1; fi

STAGING=/home/horapusa/voip-recovery-staging
TS=$(date -u +%Y%m%dT%H%M%SZ)
python3 -c "import ast; ast.parse(open('$STAGING/app.py').read())"
echo "==> app.py compiles"

if ! cmp -s "$STAGING/app.py" /opt/pbx-monitor/app.py; then
  cp -a /opt/pbx-monitor/app.py /opt/pbx-monitor/app.py.bak-geminifix-$TS
  install -o asterisk -g asterisk -m 0644 "$STAGING/app.py" /opt/pbx-monitor/app.py
  echo "==> app.py updated"
else
  echo "==> app.py unchanged"; exit 0
fi

systemctl restart pbx-monitor
sleep 1
systemctl is-active pbx-monitor && echo "==> pbx-monitor up"

echo
echo "----------------------------------------------------------------"
echo "Now in the BROWSER (you must be logged in as admin), open this URL:"
echo "  https://monitor.easmoney.me/api/flows/_gemini-health"
echo "It returns a JSON blob like:"
echo '  {"env_readable": true, "key_present": true, "key_length": 39, "key_prefix": "AIzaSy…",'
echo '   "test_call_ok": true, "test_call_text": "pong"}'
echo
echo "If test_call_ok=false, the test_call_error field tells you exactly what's wrong:"
echo "  - 'gemini HTTP 400: API_KEY_INVALID' → bad key in /opt/sampath-ai/.env"
echo "  - 'gemini HTTP 429: ...QUOTA_EXCEEDED' → free tier exhausted, wait or upgrade"
echo "  - 'gemini HTTP 404: ...gemini-2.5-pro not found' → model name issue"
echo "  - 'urlopen error ...' → network/DNS issue from this server"
echo "Paste the JSON output back and I'll diagnose."
echo "----------------------------------------------------------------"
echo "After this fix, retrying 'Generate prompts with AI' in the panel will show"
echo "the actual Gemini error message (e.g. 'gemini HTTP 429') in the modal status"
echo "line instead of the cryptic 'Unexpected token' parse error."
echo "----------------------------------------------------------------"
echo "DONE."
