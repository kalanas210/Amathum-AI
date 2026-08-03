#!/bin/bash
# install-automations.sh — deploy the Naxter Automations builder as its own systemd
# service on 127.0.0.1:5056, mirroring install-shop.sh. INDEPENDENT of the rest of the
# stack (its own dir /opt/naxter-automations + its own data /var/lib/naxter-automations);
# the module gets integrated into pbx-monitor later. Idempotent; backs up replaced files.
#
#   sudo bash install-automations.sh
#
set -euo pipefail
if [ "$EUID" -ne 0 ]; then echo "must be root (use sudo)" >&2; exit 1; fi

SRC="$(cd "$(dirname "$0")" && pwd)"          # the automation/ folder this script lives in
DEST=/opt/naxter-automations
DATA=/var/lib/naxter-automations
PORT=5056
TS=$(date -u +%Y%m%dT%H%M%SZ)
FILES="automations.py run.py requirements.txt templates/automations.html static/automations.js"

for f in $FILES; do [ -f "$SRC/$f" ] || { echo "missing $SRC/$f" >&2; exit 1; }; done

# preflight: syntax-check python + js (same gate the other installers use)
python3 -c "import ast; ast.parse(open('$SRC/automations.py').read()); ast.parse(open('$SRC/run.py').read())"
command -v node >/dev/null && node --check "$SRC/static/automations.js"
python3 -c "import flask" 2>/dev/null || { echo "ERROR: Flask not importable for /usr/bin/python3" >&2; exit 1; }
echo "==> sources parse; Flask available"

install -d -o asterisk -g asterisk -m 0755 "$DEST" "$DEST/templates" "$DEST/static" "$DATA"
for rel in $FILES; do
  src="$SRC/$rel"; dst="$DEST/$rel"
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then echo "==> $rel unchanged"; continue; fi
  [ -f "$dst" ] && cp -a "$dst" "$dst.bak-$TS"
  install -o asterisk -g asterisk -m 0644 "$src" "$dst"
  echo "==> $rel deployed"
done

cat > /etc/systemd/system/naxter-automations.service <<UNIT
[Unit]
Description=Naxter Automations (n8n-style workflow builder)
After=network.target

[Service]
Type=simple
User=asterisk
Group=asterisk
WorkingDirectory=$DEST
Environment=PORT=$PORT
Environment=AUTOMATIONS_DATA_DIR=$DATA
ExecStart=/usr/bin/python3 $DEST/run.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable naxter-automations >/dev/null 2>&1 || true
systemctl restart naxter-automations
sleep 1
if curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null; then
  echo "==> automations is up on 127.0.0.1:$PORT"
else
  echo "ERROR: did not respond on $PORT — recent logs:" >&2
  journalctl -u naxter-automations -n 25 --no-pager >&2 || true
  exit 1
fi

cat <<EOF

----------------------------------------------------------------
Automations builder deployed on 127.0.0.1:$PORT (service: naxter-automations).
Open locally: http://127.0.0.1:$PORT/automations

ONE-TIME — make it public at automations.easmoney.me (Cloudflare Zero Trust):
  Networks -> Tunnels -> (your tunnel) -> Public Hostnames -> Add a public hostname
    Subdomain: automations   Domain: easmoney.me
    Type: HTTP               URL: localhost:$PORT
  (DNS is created automatically by the tunnel.)

SECURITY: this standalone service has NO login (unlike pbx-monitor, which is
admin-gated). Before exposing it publicly, put automations.easmoney.me behind
Cloudflare Access, or integrate the module into pbx-monitor per the build guide.
Logs: journalctl -u naxter-automations -f
----------------------------------------------------------------
DONE.
EOF
