# rawos CHECKPOINT
Updated: 2026-06-09 19:12 UTC

## Current State
Phase 13 (The Inversion) COMPLETE. Full system audit done. 9 bugs fixed total.
rawos v0.6.0, port 8002, active. DB integrity: ok.

## Bugs Fixed This Session (quality audit 2026-06-09)

### Session 1 (Phase 13 deploy)
1. SERVER_SCAN trigger_block empty — agent had no anomaly context
   FIX: added SERVER_SCAN case in _run_proactive_agent() trigger_block builder

2. Cooldown granularity — all service_failed shared one 30min cooldown
   FIX: domain = f"{anomaly.kind}:{anomaly.service}" in _run_autonomous_scan()

3. First autonomous scan delayed 600s after startup
   FIX: removed initial asyncio.sleep() from autonomous_server_scan_loop()

4. SIGNAL path silently blocked — confidence default 0.6 < CONFIDENCE_THRESHOLD 0.65
   FIX: confidence threshold only gates CONTRIBUTE (code changes), not SIGNAL (observations)

### Session 2 (system audit)
5. not-found units reported as severity=8 failures (stale systemd state)
   FIX: skip parts[1]=="not-found" in _check_failed_services()

6. Orphaned executing intents never cleaned up on restart
   FIX: startup cleanup UPDATE intents SET status='failed' WHERE status='executing' AND created_at < now()-360

7. MAX_TOOL_ROUNDS=8 too low — agents cut off mid-analysis
   FIX: changed to 12 in agent_loop.py

8. research-foundry not in _SERVICE_TO_REPO — workdir="/root" — artifacts polluting /root
   FIX: added "research-foundry" -> "/root/liveproof-agent" to _SERVICE_TO_REPO
   CLEANUP: removed 25 stale RAWOS_* files from /root/

9. Colon in artifact filenames (domain used raw in filename)
   FIX: domain_safe = re.sub(r"[^a-z0-9_-]", "-", domain.lower()) in writer.py

## System State
- rawos: active, 707MB RAM (peak 817MB), healthy
- DB: 0 executing intents, integrity ok
- /root/: clean, 0 RAWOS_* pollution
- Autonomous scan: running every 600s, no initial delay
- research-foundry.service: still FAILED — rawos investigates every 30min, writes
  artifacts to /root/liveproof-agent/ with sanitized filenames
- assignee-server.service: not-found (stale), skipped by scanner
  Manual fix when convenient: systemctl reset-failed assignee-server.service

## Files Modified (full list)
- rawos/rawos/scheduler/proactive.py — patches 1-4
- rawos/rawos/context/server_scanner.py — patches 5, D1
- rawos/rawos/api/app.py — patch 6
- rawos/rawos/kernel/agent_loop.py — patch 7
- rawos/rawos/manifester/writer.py — patch D2

## Monitor Commands
systemctl status rawos
sqlite3 /root/rawos/data/rawos.db "SELECT trigger_type,domain,decision,datetime(ts,'unixepoch') FROM episodic_memory ORDER BY ts DESC LIMIT 10;"
sqlite3 /root/rawos/data/rawos.db "SELECT goal,cooldown_key,datetime(created_at,'unixepoch') FROM proactive_artifacts WHERE user_id='6eb6de1d-f5c9-4ae5-9aac-ce095b674823' ORDER BY created_at DESC LIMIT 5;"
ls /root/RAWOS_* 2>/dev/null | wc -l  # should stay 0
