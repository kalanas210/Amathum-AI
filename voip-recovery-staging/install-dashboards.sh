#!/bin/bash
# install-dashboards.sh — adds multi-industry dashboards (Reservations / Hospital
# / Sales) to the Naxter PBX monitor, attachable per-user, with a workspace
# switcher in the sidebar. Each vertical runs on local dummy data
# (static/data/<id>.js) until a real database is wired in.
#
# What it deploys:
#   app.py                         — DASHBOARDS registry, /dashboard/<industry>
#                                    route, per-user `dashboards` field on the
#                                    users API, workspace context for templates.
#   templates/base.html            — workspace switcher + per-vertical sidebar.
#   templates/users.html           — assign dashboards when adding/editing users.
#   templates/industry.html        — generic vertical dashboard shell.
#   static/industry-engine.js      — config-driven dashboard engine.
#   static/data/{reservations,hospital,sales}.js — dummy data + per-vertical config.
#
# Idempotent. Backs up replaced files as *.bak-dashboards-<ts>. Restarts pbx-monitor.
set -euo pipefail
if [ "$EUID" -ne 0 ]; then echo "must be root (use sudo)" >&2; exit 1; fi

STAGING=/home/horapusa/voip-recovery-staging
DASH=$STAGING/dashboards
DEST=/opt/pbx-monitor
TS=$(date -u +%Y%m%dT%H%M%SZ)

# --- preflight: required files present ---
need=(
  "$STAGING/app.py"
  "$DASH/templates/base.html"
  "$DASH/templates/users.html"
  "$DASH/templates/industry.html"
  "$DASH/templates/agent_mode.html"
  "$DASH/static/industry-engine.js"
  "$DASH/static/data/reservations.js"
  "$DASH/static/data/hospital.js"
  "$DASH/static/data/sales.js"
)
for f in "${need[@]}"; do
  [ -f "$f" ] || { echo "missing: $f" >&2; exit 1; }
done

# --- preflight: syntax checks ---
python3 -c "import ast; ast.parse(open('$STAGING/app.py').read())"
echo "==> app.py parses"
if command -v node >/dev/null 2>&1; then
  for j in industry-engine.js data/reservations.js data/hospital.js data/sales.js; do
    node --check "$DASH/static/$j"
  done
  echo "==> all dashboard JS passes node --check"
fi

# --- ensure static/data dir exists ---
install -d -o asterisk -g asterisk -m 0755 "$DEST/static/data"

# --- deploy (src:dst pairs), backing up only when content changed ---
pairs=(
  "$STAGING/app.py:$DEST/app.py"
  "$DASH/templates/base.html:$DEST/templates/base.html"
  "$DASH/templates/users.html:$DEST/templates/users.html"
  "$DASH/templates/industry.html:$DEST/templates/industry.html"
  "$DASH/templates/agent_mode.html:$DEST/templates/agent_mode.html"
  "$DASH/static/industry-engine.js:$DEST/static/industry-engine.js"
  "$DASH/static/data/reservations.js:$DEST/static/data/reservations.js"
  "$DASH/static/data/hospital.js:$DEST/static/data/hospital.js"
  "$DASH/static/data/sales.js:$DEST/static/data/sales.js"
)
changed=0
for p in "${pairs[@]}"; do
  src="${p%%:*}"; dst="${p##*:}"
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
    echo "==> $(basename "$dst") unchanged"
  else
    [ -f "$dst" ] && cp -a "$dst" "$dst.bak-dashboards-$TS"
    install -o asterisk -g asterisk -m 0644 "$src" "$dst"
    echo "==> $(basename "$dst") deployed${dst:+ }$([ -f "$dst.bak-dashboards-$TS" ] && echo "(backup: $(basename "$dst").bak-dashboards-$TS)")"
    changed=1
  fi
done

if [ "$changed" -eq 0 ]; then
  echo "Nothing changed — already up to date."
  exit 0
fi

systemctl restart pbx-monitor
sleep 1
systemctl is-active pbx-monitor && echo "==> pbx-monitor up"

cat <<'EOF'

----------------------------------------------------------------
Multi-industry dashboards installed.

1. Go to https://monitor.easmoney.me/users (as an admin)
   - Add or edit a user and tick the industry dashboards they should see
     (Reservations / Hospital / Sales). Admins always see all of them.
   - You can also click the dashboard chips in the users table to toggle
     access on the fly.

2. Sign in as that user (or as admin). A "Workspace" switcher appears at the
   top of the left sidebar — switch between Main PBX and each granted
   dashboard. Direct URLs: /dashboard/reservations | /dashboard/hospital |
   /dashboard/sales (403 if the user isn't granted that one).

3. Each dashboard runs on DUMMY data from /opt/pbx-monitor/static/data/<id>.js
   (no database needed yet). Edit those files to change the demo data. "Reset
   demo data" in the top bar restores the originals; changes otherwise persist
   in the browser (localStorage).

   To go live later: replace the localStorage store in industry-engine.js with
   fetch() calls to a real API, and set INDUSTRY.liveCalls = true to route the
   "call to confirm" workflow through the existing /api/make-call endpoint.
----------------------------------------------------------------
DONE.
EOF
