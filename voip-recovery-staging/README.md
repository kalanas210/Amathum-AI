# Dialog trunk recovery — fixes the two real bugs

## TL;DR

Two real bugs were diagnosed from your logs:

1. **The "No heartbeat" alert is a false alarm.** `pbx-monitor` reads `/tmp/sip-monitor.pcap` (line 27 of app.py) but the actual capture writes to `/var/log/sip-capture/sip.pcap0`. The file pbx-monitor checks doesn't exist, so the panel always shows red. **The trunk has been fine the whole time** — the reboots were unnecessary. *(Fix: `app.py` now finds the newest `sip.pcap*` from the real capture dir.)*

2. **Calls die 10 s after the agent greets.** The Sampath AI bridge stops sending PCM frames whenever Gemini is silent. Asterisk's `app_audiosocket` has a 2000 ms inactivity timeout and drops the call — what you saw as "the agent doesn't answer properly." *(Fix: `bridge.ts` now sends a 20 ms slin8 silence frame every ~500 ms when Gemini isn't talking.)*

The original soft-recovery toolchain (button + snapshots + probe + watchdog) is still included — useful for future incidents — but the two source fixes above are what actually stops the reboots and dropped calls.

## What this installs

### Source fixes (the headline)
- **`app.py`**: `tcpdump_tail()` now reads the real rolling capture (newest of `/var/log/sip-capture/sip.pcap*`)
- **`bridge.ts`**: pace timer writes a silence frame when idle > 500 ms (well under Asterisk's 2000 ms AudioSocket timeout)

### Recovery / diagnostics (defense in depth)
- **"Recover trunk" button** on `/trunk` (admin only) — runs `sip-trunk-route` restart → `sampath-ai` restart → `pjsip qualify aor NAXTER3029` → reload Asterisk if still dead. ~15 s instead of full reboot.
- **`/api/snapshots`** — captures `pjsip show endpoints`, `ip route`, `ping`, journals, pcap copy each time you click recover.
- **`voip-probe.timer`** every 10 s — pings GW + SBC, appends to `/var/log/pbx-monitor/probe.log`.
- **Event-loop watchdog** in `bridge.ts` — warns when Node.js stalls > 200 ms p99.

## Install

```
sudo bash /home/horapusa/voip-recovery-staging/install.sh
```

Idempotent: it backs up originals as `*.bak-recover-<ts>` before overwriting and re-runs cleanly.

## Verify after install

1. **Panel goes green immediately.** Open the dashboard — "Dialog trunk" should show **Alive** within ~2 s (next API refresh) instead of "No heartbeat".
2. **Test a call.** Place a call and let it sit silent for 30+ seconds without speaking. Before the fix it would die at 10 s. After the fix it should stay up until you hang up.
3. **Optional: tail the journal** to see the watchdog working:
   ```
   journalctl -u sampath-ai -f
   ```
   You should NOT see any `app_audiosocket: Reached timeout after 2000 ms` errors in `/var/log/asterisk/full` for new calls.

## Rollback

```
sudo bash /home/horapusa/voip-recovery-staging/uninstall.sh
```

Restores the most recent backups, removes systemd units, strips the sudoers block.
