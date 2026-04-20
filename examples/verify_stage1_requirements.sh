#!/usr/bin/env bash
set -euo pipefail

# Stage 1 requirement verifier for the capstone roadmap.
# Checks:
# - Ubuntu 24.04 on VMware
# - OVS installed/running
# - Mininet available
# - Python venv + Ryu + required packages
# - Optional root checks (ovs-vsctl show, mn --test pingall)

VENV_PATH="${VENV_PATH:-$HOME/sdn-env}"
PASS=0
FAIL=0
WARN=0

ok()   { echo "[PASS] $*"; PASS=$((PASS + 1)); }
bad()  { echo "[FAIL] $*"; FAIL=$((FAIL + 1)); }
warn() { echo "[WARN] $*"; WARN=$((WARN + 1)); }

echo "=== Stage 1 Requirement Verification ==="
echo

if grep -q 'VERSION_ID="24.04"' /etc/os-release; then
  ok "Ubuntu 24.04 detected"
else
  bad "Ubuntu 24.04 not detected"
fi

if [[ "$(systemd-detect-virt || true)" == "vmware" ]]; then
  ok "VM platform detected as VMware"
else
  warn "VMware not detected by systemd-detect-virt (check hypervisor manually)"
fi

if command -v ovs-vsctl >/dev/null 2>&1; then
  ok "Open vSwitch CLI installed ($(ovs-vsctl --version | head -n1))"
else
  bad "ovs-vsctl not installed"
fi

if [[ "$(systemctl is-active openvswitch-switch 2>/dev/null || true)" == "active" ]]; then
  ok "openvswitch-switch service is active"
else
  bad "openvswitch-switch service is not active"
fi

if command -v mn >/dev/null 2>&1; then
  MN_VER="$(mn --version 2>&1 | tr -d '\r\n' || true)"
  if [[ -z "${MN_VER}" || "${MN_VER}" == "unknown" ]]; then
    MN_VER="$(python3 - <<'PY' 2>/dev/null || true
from mininet.net import VERSION
print(VERSION)
PY
)"
    MN_VER="$(echo "${MN_VER}" | tr -d '\r\n')"
  fi
  [[ -n "${MN_VER}" ]] || MN_VER="unknown"
  ok "Mininet installed (version: ${MN_VER})"
else
  bad "mn command not found"
fi

if [[ -f "${VENV_PATH}/bin/activate" ]]; then
  ok "Python virtualenv found at ${VENV_PATH}"
else
  bad "Virtualenv not found at ${VENV_PATH}"
fi

if [[ -f "${VENV_PATH}/bin/activate" ]]; then
  # shellcheck disable=SC1090
  source "${VENV_PATH}/bin/activate"

  if command -v ryu-manager >/dev/null 2>&1; then
    ok "Ryu installed ($(ryu-manager --version 2>/dev/null || echo unknown))"
  else
    bad "ryu-manager not found in virtualenv"
  fi

  if python3 - <<'PY' >/tmp/stage1_pkg_check.out 2>&1
import importlib
mods=["flask","numpy","torch","ryu","eventlet"]
missing=[]
for m in mods:
    try:
        importlib.import_module(m)
    except Exception:
        missing.append(m)
if missing:
    raise SystemExit("missing:" + ",".join(missing))
print("ok")
PY
  then
    ok "Required Python packages installed (flask, numpy, torch, ryu, eventlet)"
  else
    bad "Missing Python packages: $(cat /tmp/stage1_pkg_check.out)"
  fi

  if python3 -m pip check >/tmp/stage1_pip_check.out 2>&1; then
    ok "pip dependency check clean"
  else
    bad "pip dependency issues: $(tail -n 5 /tmp/stage1_pip_check.out)"
  fi
fi

if sudo -n true 2>/dev/null; then
  if sudo ovs-vsctl show >/tmp/stage1_ovs_show.out 2>&1; then
    ok "ovs-vsctl show works with sudo"
  else
    bad "sudo ovs-vsctl show failed: $(tail -n 3 /tmp/stage1_ovs_show.out)"
  fi

  if sudo mn --test pingall >/tmp/stage1_mn_pingall.out 2>&1; then
    ok "Mininet starts successfully (mn --test pingall)"
  else
    bad "sudo mn --test pingall failed: $(tail -n 6 /tmp/stage1_mn_pingall.out)"
  fi
else
  warn "Skipped sudo checks (no non-interactive sudo). Run manually:"
  warn "  sudo ovs-vsctl show"
  warn "  sudo mn --test pingall"
fi

echo
echo "Summary: PASS=${PASS} FAIL=${FAIL} WARN=${WARN}"
if [[ "${FAIL}" -gt 0 ]]; then
  exit 1
fi
exit 0
