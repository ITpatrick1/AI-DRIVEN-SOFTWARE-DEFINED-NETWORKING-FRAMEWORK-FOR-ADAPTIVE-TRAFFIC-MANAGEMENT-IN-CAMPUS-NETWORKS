#!/usr/bin/env bash
set -euo pipefail

# Install Campus SDN dependencies from a previously prepared offline bundle.

VENV_PATH="${VENV_PATH:-${HOME}/sdn-env}"
BUNDLE_DIR="${1:-${HOME}/campus-offline-bundle}"
APT_DIR="${BUNDLE_DIR}/apt"
PIP_DIR="${BUNDLE_DIR}/pip"
REQ_RUNTIME="${BUNDLE_DIR}/requirements-runtime.txt"

if [[ ! -d "${APT_DIR}" ]]; then
  echo "Missing apt bundle directory: ${APT_DIR}"
  exit 1
fi
if [[ ! -d "${PIP_DIR}" ]]; then
  echo "Missing pip bundle directory: ${PIP_DIR}"
  exit 1
fi
if [[ ! -f "${REQ_RUNTIME}" ]]; then
  echo "Missing requirements file: ${REQ_RUNTIME}"
  exit 1
fi

shopt -s nullglob
APT_DEBS=("${APT_DIR}"/*.deb)
shopt -u nullglob
if [[ "${#APT_DEBS[@]}" -eq 0 ]]; then
  echo "No .deb files found in ${APT_DIR}"
  exit 1
fi
if [[ -z "$(find "${PIP_DIR}" -maxdepth 1 -type f -print -quit)" ]]; then
  echo "No Python package artifacts found in ${PIP_DIR}"
  exit 1
fi

echo "[1/5] Staging apt packages into local cache..."
sudo install -d /var/cache/apt/archives/partial
sudo cp -f "${APT_DEBS[@]}" /var/cache/apt/archives/

echo "[2/5] Installing apt packages from local bundle..."
if ! sudo dpkg -i "${APT_DEBS[@]}"; then
  sudo apt-get --no-download -f install -y
fi

echo "[3/5] Ensuring virtualenv exists at ${VENV_PATH}..."
if [[ ! -f "${VENV_PATH}/bin/activate" ]]; then
  python3 -m venv "${VENV_PATH}"
fi

echo "[4/5] Installing Python packages from local bundle..."
source "${VENV_PATH}/bin/activate"
python3 -m ensurepip --upgrade >/dev/null 2>&1 || true
python3 -m pip install --no-index --find-links "${PIP_DIR}" -r "${REQ_RUNTIME}"

echo "[5/5] Verifying key commands..."
ryu-manager --version
iperf3 --version | head -n 1
sudo mn --version
echo "Offline installation complete."
