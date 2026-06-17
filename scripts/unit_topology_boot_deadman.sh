#!/bin/bash
# rawos Phase 23F.3 boot deadman — I-UT8 (v2: network-online.target + retry loop)
# Runs at boot if /etc/rawos/unit-topology-deadman.armed exists.
# Retry up to 60s for floor units to become active (prevents false positive from ordering race).
# Healthy floor → disarm self. Unhealthy floor OR force-revert flag → revert + reboot.
set -uo pipefail

ARMED="/etc/rawos/unit-topology-deadman.armed"
REVERT="/etc/rawos/unit-topology-deadman.revert.sh"
FORCE_REVERT="/etc/rawos/unit-topology-deadman.force-revert"

echo "rawos-unit-topology-revert: armed — running boot health check"

# Drill mode: force-revert flag bypasses floor check (no need to break floor for testing)
if [[ -f "$FORCE_REVERT" ]]; then
    echo "FORCE-REVERT flag present — executing revert (drill)"
    rm -f "$FORCE_REVERT" "$ARMED"
    if [[ -f "$REVERT" ]]; then
        bash "$REVERT"
    fi
    systemctl disable rawos-unit-topology-revert.service --no-reload 2>/dev/null || true
    systemctl daemon-reload
    echo "Revert complete. Rebooting in 5s..."
    sleep 5
    reboot
    exit 0
fi

# Floor health check with retry loop — up to 60s for floor units to fully start
# network-online.target ordering still has a race window with ssh.service startup
declare -a FLOOR_CHECKS=("ssh.service" "rawos.service" "systemd-networkd.service")
MAX_WAIT_S=60
INTERVAL_S=5
MAX_ATTEMPTS=12
UNHEALTHY=0

for attempt in $(seq 1 $MAX_ATTEMPTS); do
    UNHEALTHY=0
    for unit in "${FLOOR_CHECKS[@]}"; do
        if ! systemctl is-active --quiet "$unit" 2>/dev/null; then
            UNHEALTHY=1
            echo "  Attempt $attempt/$MAX_ATTEMPTS: $unit not active yet — waiting ${INTERVAL_S}s..."
            break
        fi
    done
    if [[ $UNHEALTHY -eq 0 ]]; then
        break
    fi
    sleep $INTERVAL_S
done

# Final state report
for unit in "${FLOOR_CHECKS[@]}"; do
    if systemctl is-active --quiet "$unit" 2>/dev/null; then
        echo "  HEALTHY: $unit"
    else
        echo "  UNHEALTHY: $unit (after ${MAX_WAIT_S}s wait)"
        UNHEALTHY=1
    fi
done

if [[ $UNHEALTHY -eq 0 ]]; then
    echo "All floor units healthy — disarming deadman"
    rm -f "$ARMED"
    systemctl disable rawos-unit-topology-revert.service --no-reload 2>/dev/null || true
    systemctl daemon-reload
    echo "rawos-unit-topology-revert: disarmed successfully"
    exit 0
fi

# Floor unhealthy after full retry window — revert and reboot
echo "FLOOR UNHEALTHY after ${MAX_WAIT_S}s — executing revert before reboot"
rm -f "$ARMED"
if [[ -f "$REVERT" ]]; then
    bash "$REVERT" && echo "Revert script executed OK" || echo "WARNING: revert script exit non-zero"
fi
systemctl disable rawos-unit-topology-revert.service --no-reload 2>/dev/null || true
systemctl daemon-reload
echo "Rebooting in 5s..."
sleep 5
reboot
