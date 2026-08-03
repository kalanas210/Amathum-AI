#!/bin/bash
# install-shop.sh — deploy the Naxter storefront (shop.easmoney.me) as its own
# systemd service on 127.0.0.1:5055. It reads the shared product catalogue
# (/var/lib/sampath-ai/refdata/sales.json) and writes web orders into the Sales
# dashboard's order store, so a checkout shows on the dashboard within seconds
# and enters the AI confirmation-call queue. Idempotent; backs up replaced files.
set -euo pipefail
if [ "$EUID" -ne 0 ]; then echo "must be root (use sudo)" >&2; exit 1; fi

STAGING=/home/horapusa/voip-recovery-staging
SRC=$STAGING/shop
DEST=/opt/naxter-shop
DATA=/var/lib/sampath-ai
PORT=5055
TS=$(date -u +%Y%m%dT%H%M%SZ)

[ -f "$SRC/shop.py" ] || { echo "missing $SRC/shop.py" >&2; exit 1; }
[ -f "$SRC/templates/store.html" ] || { echo "missing $SRC/templates/store.html" >&2; exit 1; }
python3 -c "import ast; ast.parse(open('$SRC/shop.py').read())"
echo "==> shop.py parses"

install -d -o asterisk -g asterisk -m 0755 "$DEST" "$DEST/templates"
for rel in shop.py templates/store.html; do
  src="$SRC/$rel"; dst="$DEST/$rel"
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then echo "==> $rel unchanged"; continue; fi
  [ -f "$dst" ] && cp -a "$dst" "$dst.bak-$TS"
  install -o asterisk -g asterisk -m 0644 "$src" "$dst"
  echo "==> $rel deployed"
done

# the shared sales order store (also created by install-agents.sh)
install -d -o asterisk -g asterisk -m 0755 "$DATA/bookings" "$DATA/bookings/sales" "$DATA/bookings/sales/orders"

cat > /etc/systemd/system/naxter-shop.service <<UNIT
[Unit]
Description=Naxter Store (public storefront for the Sales vertical)
After=network.target

[Service]
Type=simple
User=asterisk
Group=asterisk
WorkingDirectory=$DEST
Environment=SHOP_PORT=$PORT
Environment=SAMPATH_DATA_DIR=$DATA
ExecStart=/usr/bin/python3 $DEST/shop.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable naxter-shop >/dev/null 2>&1 || true
systemctl restart naxter-shop
sleep 1
if curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null; then
  echo "==> shop is up on 127.0.0.1:$PORT"
else
  echo "ERROR: shop did not respond on $PORT — recent logs:" >&2
  journalctl -u naxter-shop -n 25 --no-pager >&2 || true
  exit 1
fi

cat <<EOF

----------------------------------------------------------------
Storefront deployed on 127.0.0.1:$PORT (service: naxter-shop).

ONE-TIME — make it public at shop.easmoney.me (Cloudflare Zero Trust):
  Networks -> Tunnels -> (your tunnel) -> Public Hostnames -> Add a public hostname
    Subdomain: shop     Domain: easmoney.me
    Type: HTTP          URL: localhost:$PORT
  (DNS is created automatically by the tunnel.)

Then open https://shop.easmoney.me, place an order, and watch it appear on the
Sales dashboard within ~4s (and in the AI confirmation-call queue).
Logs: journalctl -u naxter-shop -f
----------------------------------------------------------------
DONE.
EOF
