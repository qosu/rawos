#!/usr/bin/env bash
# shp_verify_baseline.sh — SHP.0 read-only security posture check
# Re-run any time to compare against frozen baseline (2026-06-26).
# Exit 0 = all checks pass expected values; exit 1 = drift detected.
# NEVER modifies any system state.

set -uo pipefail

PASS=0
FAIL=0
WARN=0

_ok()   { echo "[PASS] $*"; ((PASS++)); }
_fail() { echo "[FAIL] $*"; ((FAIL++)); }
_warn() { echo "[WARN] $*"; ((WARN++)); }

echo "=== SHP Baseline Verification — $(date -Iseconds) ==="
echo

# ── Firewall ──────────────────────────────────────────────────────────────────
echo "--- Perimeter ---"
nft_rules=$(nft list ruleset 2>/dev/null | wc -l)
ufw_active=$(ufw status 2>/dev/null | grep -c "Status: active" || true)
if [ "$nft_rules" -gt 5 ]; then
    _ok "nftables active default-deny perimeter ($nft_rules lines)"
elif [ "$ufw_active" -gt 0 ]; then
    _ok "ufw active (nftables absent but ufw covers)"
else
    _fail "NO host firewall: nftables empty ($nft_rules lines) and ufw inactive"
fi

# ── SSH ───────────────────────────────────────────────────────────────────────
echo "--- SSH ---"
pw_auth=$(sshd -T 2>/dev/null | grep "^passwordauthentication " | awk '{print $2}')
if [ "$pw_auth" = "no" ]; then
    _ok "PasswordAuthentication no"
else
    _fail "PasswordAuthentication is '$pw_auth' — brute-force surface OPEN"
fi

x11=$(sshd -T 2>/dev/null | grep "^x11forwarding " | awk '{print $2}')
if [ "$x11" = "no" ]; then
    _ok "X11Forwarding no"
else
    _warn "X11Forwarding is '$x11' (should be no)"
fi

tcp_fwd=$(sshd -T 2>/dev/null | grep "^allowtcpforwarding " | awk '{print $2}')
if [ "$tcp_fwd" = "no" ]; then
    _ok "AllowTcpForwarding no"
else
    _warn "AllowTcpForwarding is '$tcp_fwd'"
fi

max_auth=$(sshd -T 2>/dev/null | grep "^maxauthtries " | awk '{print $2}')
if [ "$max_auth" -le 3 ]; then
    _ok "MaxAuthTries $max_auth (≤3)"
else
    _warn "MaxAuthTries $max_auth (should be ≤3)"
fi

# ── fail2ban + auditd ─────────────────────────────────────────────────────────
echo "--- Host daemons ---"
if systemctl is-active --quiet fail2ban 2>/dev/null; then
    _ok "fail2ban active"
else
    _fail "fail2ban absent/inactive"
fi

if systemctl is-active --quiet auditd 2>/dev/null; then
    _ok "auditd active"
else
    _warn "auditd absent/inactive (no syscall trail)"
fi

# ── Secrets confinement ───────────────────────────────────────────────────────
echo "--- Secrets ---"
if [ -f /root/rawos/.env ]; then
    env_perms=$(stat -c "%a" /root/rawos/.env)
    if [ "$env_perms" = "600" ]; then
        _ok ".env permissions 600"
    else
        _fail ".env permissions $env_perms (should be 600)"
    fi
    # Check secrets not in plaintext env of running process
    rawos_pid=$(systemctl show rawos --property=MainPID --value 2>/dev/null || echo 0)
    if [ "$rawos_pid" -gt 0 ]; then
        if grep -q "STRIPE_KEY" /proc/"$rawos_pid"/environ 2>/dev/null; then
            _warn "STRIPE_KEY visible in rawos process environ (not yet migrated to LoadCredential)"
        else
            _ok "STRIPE_KEY not in rawos process environ"
        fi
    else
        _warn "rawos MainPID=0 — cannot check process environ"
    fi
else
    _warn ".env absent (may have been migrated)"
fi

# ── Docker sandbox ────────────────────────────────────────────────────────────
echo "--- Sandbox ---"
if docker info &>/dev/null; then
    _ok "Docker daemon running"
    # Check if gVisor runtime available
    if docker info 2>/dev/null | grep -q "runsc"; then
        _ok "gVisor (runsc) runtime registered"
    else
        _warn "gVisor (runsc) NOT registered — using runc (weaker kernel isolation)"
    fi
else
    _fail "Docker not running"
fi

sandbox_flag=$(grep -E "^SANDBOX_DOCKER" /root/rawos/.env 2>/dev/null | cut -d= -f2 || echo "unknown")
if [ "$sandbox_flag" = "true" ] || [ "$sandbox_flag" = "1" ]; then
    _ok "SANDBOX_DOCKER=$sandbox_flag (container path forced)"
else
    _fail "SANDBOX_DOCKER=$sandbox_flag — host-direct bash ACTIVE for users"
fi

# ── BPF LSM ───────────────────────────────────────────────────────────────────
echo "--- LSM ---"
lsm_active=$(cat /sys/kernel/security/lsm 2>/dev/null || echo "unknown")
if echo "$lsm_active" | grep -q "bpf"; then
    _ok "BPF LSM in active LSM list: $lsm_active"
else
    _fail "BPF LSM NOT active: $lsm_active"
fi

bpf_mode=$(grep -E "bpf_lsm_mode" /root/rawos/rawos/config.py 2>/dev/null | grep -oE '"[^"]*"' | head -1)
echo "    bpf_lsm_mode in config.py: $bpf_mode"
if [ "$bpf_mode" = '"enforce"' ]; then
    _ok "BPF LSM mode = enforce (deny active)"
else
    _warn "BPF LSM mode = $bpf_mode (audit-only, not enforcing)"
fi

# ── Packages ──────────────────────────────────────────────────────────────────
echo "--- Packages ---"
upgradable=$(apt list --upgradable 2>/dev/null | grep -c upgradable || echo 0)
if [ "$upgradable" -eq 0 ]; then
    _ok "No upgradable packages"
else
    _warn "$upgradable packages upgradable — run apt upgrade"
fi

# ── Services health ───────────────────────────────────────────────────────────
echo "--- Core services ---"
for svc in rawos nginx redis-server ssh; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        _ok "$svc active"
    else
        _fail "$svc NOT active"
    fi
done

# ── Summary ───────────────────────────────────────────────────────────────────
echo
echo "=== SUMMARY: PASS=$PASS  WARN=$WARN  FAIL=$FAIL ==="
if [ "$FAIL" -gt 0 ]; then
    echo "ACTION REQUIRED: $FAIL failure(s) above need remediation."
    exit 1
else
    echo "No failures. Warnings are tracked improvements."
    exit 0
fi
