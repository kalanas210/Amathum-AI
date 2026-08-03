#!/bin/bash
# /opt/pbx-monitor/snapshot.sh — capture VoIP diagnostic state.
# Called by /api/soft-recover BEFORE any restart so we capture the broken state.
# Usage: snapshot.sh [label]    (label is included in the directory name)
set -u

LABEL="${1:-manual}"
TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT="/var/log/pbx-monitor/snapshots/${TS}_${LABEL}"
mkdir -p "$OUT"
exec 2>>"$OUT/snapshot.err"

run() {
  local name="$1"; shift
  { echo "# $ $*"; "$@"; echo "# rc=$?"; } >"$OUT/$name" 2>&1
}

# --- Asterisk state ---
run asterisk-pjsip-endpoints  asterisk -rx 'pjsip show endpoints'
run asterisk-pjsip-contacts   asterisk -rx 'pjsip show contacts'
run asterisk-pjsip-aors       asterisk -rx 'pjsip show aors'
run asterisk-sip-peers        asterisk -rx 'sip show peers'
run asterisk-channels         asterisk -rx 'core show channels'
run asterisk-uptime           asterisk -rx 'core show uptime'

# --- Network state ---
run ip-route-sbc              ip route get 10.10.10.89
run ip-route-gw               ip route get 192.168.1.254
run ip-addr-enp4s0            ip -br addr show enp4s0
run ss-sip-audiosocket        sh -c "ss -tunp | grep -E ':5060|:9090|:9091' || true"
run ping-gw                   ping -c 3 -W 1 192.168.1.254
run ping-sbc                  ping -c 3 -W 1 10.10.10.89

# --- Service journals (last 200 lines, no pager) ---
run journal-asterisk          journalctl -u asterisk        -n 200 --no-pager
run journal-sampath-ai        journalctl -u sampath-ai      -n 200 --no-pager
run journal-sip-trunk-route   journalctl -u sip-trunk-route -n 50  --no-pager
run journal-pbx-monitor       journalctl -u pbx-monitor     -n 100 --no-pager

# --- Host health ---
run free                      free -h
run uptime                    uptime
run df                        df -h /var /tmp
run dmesg-tail                sh -c "dmesg -T 2>/dev/null | tail -50 || true"

# --- SIP capture snapshot (copy, don't move) ---
if [ -r /tmp/sip-monitor.pcap ]; then
  cp /tmp/sip-monitor.pcap "$OUT/sip-monitor.pcap" 2>>"$OUT/snapshot.err"
fi

# --- Recent probe log slice ---
if [ -r /var/log/pbx-monitor/probe.log ]; then
  tail -n 200 /var/log/pbx-monitor/probe.log >"$OUT/probe-tail.log" 2>>"$OUT/snapshot.err"
fi

# Manifest for the panel
ls -la "$OUT" >"$OUT/MANIFEST"
echo "$OUT"
