#!/bin/bash
# install-ryzera-monitor.sh — (re)build the RYZERA-branded clone of the PBX monitor
# as a SEPARATE instance (own dir, own systemd unit, own port 5052), served at
# https://ryzera.easmoney.me via a Cloudflare tunnel public hostname.
#
# It re-clones from the LIVE /opt/pbx-monitor so ryzera.easmoney.me is byte-for-byte
# current with monitor.easmoney.me, then applies the only two intentional differences:
#   - a site file (/etc/pbx-monitor/site-ryzera.json) supplying port 5052 + the
#     "Ryzera PBX" display name, wired in via Environment=PBX_MONITOR_SITE
#   - a sed over the three templates that still hard-code the name in HTML
#
# It does NOT touch the live naxter monitor (monitor.easmoney.me / pbx-monitor).
#
# --- 2026-08-03: app.py is no longer patched at all -------------------------
# Previous versions string-replaced BASE, the snapshot path and the listen port
# inside the cloned app.py. Every one of those has now been removed, because
# every one of them broke (or was silently wrong) when app.py was refactored:
#
#   * BASE           — dropped 2026-07-29. app.py reads it from $PBX_MONITOR_BASE.
#   * port=5051      — dropped 2026-08-03. The 2026-08-02 unified-site refactor
#                      replaced `app.run(..., port=5051)` with `_bind_address()`
#                      reading SITE['bind']['port'], so the literal "port=5051"
#                      no longer exists anywhere. The old replace matched nothing
#                      and its assert would have ABORTED the script after the old
#                      clone had already been moved aside — taking ryzera down.
#   * snapshot.sh    — dropped 2026-08-03. The replace still matched, but it
#                      repointed SNAPSHOT_SH at /opt/pbx-monitor-ryzera/snapshot.sh
#                      while /etc/sudoers.d/pbx-monitor only grants NOPASSWD on
#                      /opt/pbx-monitor/snapshot.sh — so soft-recover on ryzera was
#                      failing on sudo. Both instances are on the same host and drive
#                      the same Asterisk, so the canonical path is the correct one.
#
# The rule this encodes: configure the clone through the app's own supported
# override mechanisms (PBX_MONITOR_BASE / PBX_MONITOR_SITE), never by pattern
# matching its source. app.py in the clone is now identical to the live one, and
# the script asserts that at the end.
#
# Idempotent: builds + validates a fresh clone in a staging dir and only swaps it
# into place once it passes, so a failed run leaves the running ryzera untouched.
set -euo pipefail
if [ "$EUID" -ne 0 ]; then echo "must be root (use sudo)" >&2; exit 1; fi

SRC=/opt/pbx-monitor
DEST=/opt/pbx-monitor-ryzera
STAGE=/opt/.pbx-monitor-ryzera.staging
PORT=5052
SVC=pbx-monitor-ryzera
SITE_DIR=/etc/pbx-monitor
SITE_JSON="$SITE_DIR/site-ryzera.json"
TS=$(date -u +%Y%m%dT%H%M%SZ)

[ -d "$SRC" ] || { echo "source $SRC not found" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 required" >&2; exit 1; }

# --- 1) build a fresh clone in staging (running instance untouched) ---------
rm -rf "$STAGE"
echo "==> cloning $SRC -> $STAGE"
cp -a "$SRC" "$STAGE"

# drop derived cruft and the accumulated app.py.bak-* history from the clone
rm -rf "$STAGE/__pycache__" "$STAGE/templates/__pycache__" 2>/dev/null || true
find "$STAGE" -maxdepth 2 -name '*.bak-*' -type f -delete 2>/dev/null || true

# --- 2) app.py is copied verbatim — assert we really did not touch it -------
if ! cmp -s "$SRC/app.py" "$STAGE/app.py"; then
  echo "!! clone app.py differs from live app.py — aborting, ryzera untouched" >&2
  rm -rf "$STAGE"; exit 1
fi
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$STAGE/app.py" \
  && echo "==> clone app.py is identical to live and parses"

# --- 3) site file: the ONLY place port + display name are configured --------
# Deep-merged over SITE_DEFAULTS by app.py, so every key NOT listed here keeps
# the exact home-box value — that is what guarantees parity with monitor.
mkdir -p "$SITE_DIR"
cat > "$SITE_JSON" <<'SITEEOF'
{
  "site_id": "ryzera",
  "site_name": "Ryzera PBX",
  "bind": { "host": "127.0.0.1", "port": 5052 }
}
SITEEOF
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); assert d['bind']['port']==5052" "$SITE_JSON"
echo "==> wrote $SITE_JSON (port 5052, name 'Ryzera PBX'); everything else inherits home defaults"

# NOTE: we deliberately do NOT create $SITE_DIR/site.json — that is the path the
# home instance reads. It must stay absent so monitor.easmoney.me keeps using the
# built-in SITE_DEFAULTS and is completely unaffected by this script.
if [ -e "$SITE_DIR/site.json" ]; then
  echo "!! WARNING: $SITE_DIR/site.json exists — the HOME instance reads that file." >&2
  echo "   This script did not create it. Verify it is intentional." >&2
fi

# --- 4) UI rebrand: the templates that still hard-code the name in HTML ------
# base.html:226 renders {{ site.site_name }} and is covered by the site file above;
# these are the remaining literal occurrences (<title> tags and the login blurb).
sed -i 's/Naxter PBX/Ryzera PBX/g; s#Naxter</title>#Ryzera</title>#' "$STAGE/templates/base.html"
sed -i 's/Naxter PBX/Ryzera PBX/g' "$STAGE/templates/login.html"
sed -i 's/Naxter PBX/Ryzera PBX/g' "$STAGE/templates/index.html"
for f in base login index; do
  if grep -q 'Naxter' "$STAGE/templates/$f.html"; then
    echo "!! 'Naxter' still present in $f.html after rebrand — aborting, ryzera untouched" >&2
    grep -n 'Naxter' "$STAGE/templates/$f.html" >&2
    rm -rf "$STAGE"; exit 1
  fi
done
echo "==> rebranded base/login/index to 'Ryzera PBX'"
# Left as naxter on purpose (spoken company identity / telco-side IDs, same policy
# as the outbound caller-ID): the TTS default scripts in make_call.html and
# broadcast.html ("this is Naxter A I Solutions").

# --- 5) ownership (runs as the asterisk service user) -----------------------
chown -R asterisk:asterisk "$STAGE"
chmod 0644 "$SITE_JSON"

# --- 6) swap staging into place (old clone preserved) -----------------------
if [ -e "$DEST" ]; then
  echo "==> preserving current clone as $DEST.bak-$TS"
  mv "$DEST" "$DEST.bak-$TS"
fi
mv "$STAGE" "$DEST"

# --- 7) systemd unit --------------------------------------------------------
cat > "/etc/systemd/system/$SVC.service" <<UNITEOF
[Unit]
Description=Ryzera PBX Monitor (Flask dashboard)
After=network-online.target asterisk.service
Wants=network-online.target

[Service]
Type=simple
User=asterisk
Group=asterisk
Environment=PBX_MONITOR_BASE=$DEST
Environment=PBX_MONITOR_SITE=$SITE_JSON
WorkingDirectory=$DEST
ExecStart=/usr/bin/python3 $DEST/app.py
Restart=on-failure
RestartSec=5
StartLimitInterval=0

[Install]
WantedBy=multi-user.target
UNITEOF

systemctl daemon-reload
systemctl enable "$SVC" >/dev/null 2>&1 || true
systemctl restart "$SVC"
sleep 2

# --- 8) health + parity checks ----------------------------------------------
if systemctl is-active --quiet "$SVC"; then
  echo "==> $SVC active on 127.0.0.1:$PORT"
else
  echo "!! $SVC failed to start — recent logs:" >&2
  journalctl -u "$SVC" -n 40 --no-pager >&2 || true
  echo "!! previous clone is preserved at $DEST.bak-$TS" >&2
  exit 1
fi

code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/" || true)
echo "==> local probe http://127.0.0.1:$PORT/ -> HTTP $code (200 or 302 = healthy)"
[ "$code" = "200" ] || [ "$code" = "302" ] || { echo "!! unhealthy response" >&2; exit 1; }

# confirm ryzera really is listening on 5052 (i.e. the site file took effect)
ss -ltnp 2>/dev/null | grep -q "127.0.0.1:$PORT" \
  && echo "==> confirmed listening on 127.0.0.1:$PORT" \
  || echo "!! could not confirm listener on $PORT (check 'ss -ltnp')" >&2

# the parity guarantee, stated as a check rather than a claim
echo "==> parity check (app.py):"
md5sum "$SRC/app.py" "$DEST/app.py" | sed 's/^/    /'
cmp -s "$SRC/app.py" "$DEST/app.py" \
  && echo "    IDENTICAL — ryzera is running the same code as monitor" \
  || { echo "!! app.py MISMATCH" >&2; exit 1; }

# the home instance must be untouched
mcode=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:5051/" || true)
echo "==> monitor.easmoney.me backend still on 5051 -> HTTP $mcode (unchanged by this script)"

nbak=$(ls -d "$DEST".bak-* 2>/dev/null | wc -l)
echo "==> $nbak preserved backup clone(s): $(du -sh --total $DEST.bak-* 2>/dev/null | tail -1 | cut -f1) total"

cat <<EOF

----------------------------------------------------------------
ryzera.easmoney.me is resynced. It is now running the SAME app.py,
templates and static assets as monitor.easmoney.me — including every
feature shipped since the last sync (Holton rename, multi-industry
dashboards, the Hemas NPS survey, the unified site-config refactor).

The only differences, both by design:
  - display name "Ryzera PBX" (site file + 3 templates)
  - listens on 127.0.0.1:$PORT instead of 5051

No Cloudflare change is needed — the ryzera public hostname already
points at localhost:$PORT. Just open https://ryzera.easmoney.me
(hard-refresh / Ctrl-Shift-R to get past cached CSS+JS).

Logins were re-seeded from /opt/pbx-monitor/instance, so ryzera's users
are monitor's current users again. The previous clone (with its old
users) is preserved at:
  $DEST.bak-$TS

Prune old backups when you are happy:
  rm -rf $DEST.bak-*

Rollback to the previous clone:
  systemctl stop $SVC
  rm -rf $DEST && mv $DEST.bak-$TS $DEST
  systemctl start $SVC
----------------------------------------------------------------
DONE.
EOF
