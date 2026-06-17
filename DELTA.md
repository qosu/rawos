CHANGED: scripts/unit_topology_boot_deadman.sh | v2: 60s retry loop (prevents ssh.service ordering race false positive) | I-UT8 robust
CHANGED: scripts/systemd/rawos-unit-topology-revert.service | Wants=network-online.target added | prevents race
DRILL RESULT: 23F.3 COMPLETE — 3 reboots total: false-positive fixed, correct-disarm, force-revert-drill all PASS
NEXT: 23F.4 human gate — graduate runtime ops (author+delete), GRADUATION_THRESHOLD*2=6 verified successes needed
