#!/bin/bash
# install-esc-fix.sh — one-liner fix for ReferenceError: esc is not defined.
# The HTML-escape helper in flows.js is named `escape()`; my v2/v3 additions
# call it as `esc()`. Adds `const esc = escape;` alias so both names work.
set -euo pipefail
if [ "$EUID" -ne 0 ]; then echo "must be root (use sudo)" >&2; exit 1; fi
STAGING=/home/horapusa/voip-recovery-staging
TS=$(date -u +%Y%m%dT%H%M%SZ)
src=$STAGING/flows/static/flows.js
dst=/opt/pbx-monitor/static/flows.js
if ! cmp -s "$src" "$dst"; then
  cp -a "$dst" "$dst.bak-escfix-$TS"
  install -o asterisk -g asterisk -m 0644 "$src" "$dst"
  echo "==> flows.js updated (backup: $(basename "$dst").bak-escfix-$TS)"
else
  echo "==> flows.js unchanged"
fi
# Static asset — no pbx-monitor restart needed, just hard-reload the browser.
echo "DONE — hard-reload /flows in your browser (Ctrl+Shift+R / Cmd+Shift+R)."
