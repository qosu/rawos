CHANGED: rawos/config.py | SHP.2 I-SEC3: _SystemdCredentialsSource reads /run/credentials/rawos.service/ (via CREDENTIALS_DIRECTORY env — only set by systemd in service process, not test contexts) | SHP.2 COMPLETE
CHANGED: rawos/api/app.py | SHP.6: flip_mode(settings.bpf_lsm_mode) wired at startup; SHP.7: dep drift check + audit chain startup record extended | SHP.6 LIVE enforce mode
STATUS: SHP.2 (secrets migration) DONE; SHP.6 (BPF LSM enforce) LIVE on prod; SHP.7 COMMITTED f19ca138; 1331 tests green
NEXT: SHP.3/SHP.4 commit (sandbox.py cap-drop/read-only, capability_gate, output_guard — already implemented, not yet committed)
