#!/bin/bash
# /opt/pbx-monitor/voip-probe.sh
# Pings the Huawei AR (192.168.1.254) and Dialog SBC (10.10.10.89), one packet each,
# writes a single CSV line to /var/log/pbx-monitor/probe.log.
# Format: iso_ts,gw_loss,gw_rtt_ms,sbc_loss,sbc_rtt_ms
set -u

LOG=/var/log/pbx-monitor/probe.log
mkdir -p "$(dirname "$LOG")"

probe() {
  # $1 = host. Returns "loss_pct,rtt_ms" or "100,-" on total loss.
  local out
  out=$(ping -c 1 -W 1 -q "$1" 2>/dev/null | tail -2)
  local loss rtt
  loss=$(echo "$out" | grep -oE '[0-9]+% packet loss' | grep -oE '[0-9]+' | head -1)
  rtt=$(echo "$out"  | grep -oE 'rtt[^=]*= [^/]+/[^/]+' | awk -F'= ' '{print $2}' | awk -F/ '{print $2}')
  echo "${loss:-100},${rtt:--}"
}

GW=$(probe 192.168.1.254)
SBC=$(probe 10.10.10.89)
TS=$(date -u --iso-8601=seconds)

echo "${TS},${GW},${SBC}" >>"$LOG"
