#!/usr/bin/env bash
set -euo pipefail

# Prepare an offline bundle for Campus SDN development/demo.
# - Installs required runtime packages now (while online)
# - Downloads apt .deb artifacts
# - Downloads pip wheels/sdists for the active virtualenv
# - Builds a local Ryu wheel from ~/ryu-src if available

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_PATH="${VENV_PATH:-${HOME}/sdn-env}"
BUNDLE_DIR="${1:-${HOME}/campus-offline-bundle}"
APT_DIR="${BUNDLE_DIR}/apt"
PIP_DIR="${BUNDLE_DIR}/pip"
REQ_RUNTIME="${BUNDLE_DIR}/requirements-runtime.txt"
REQ_DOWNLOAD="${BUNDLE_DIR}/requirements-download.txt"
RYU_SOURCE_DIR="${RYU_SOURCE_DIR:-${HOME}/ryu-src}"

APT_PACKAGES=(
  openvswitch-switch
  iperf3
  tcpdump
  curl
  net-tools
  iproute2
  iputils-ping
  python3-pip
  python3-venv
  git
  jq
)

if [[ ! -f "${VENV_PATH}/bin/activate" ]]; then
  echo "Virtualenv not found at ${VENV_PATH}"
  exit 1
fi

mkdir -p "${APT_DIR}" "${PIP_DIR}"

echo "[1/7] Installing required apt runtime packages..."
sudo apt-get update
sudo apt-get install -y "${APT_PACKAGES[@]}"

echo "[2/7] Downloading apt packages for offline reinstall..."
sudo apt-get install --reinstall --download-only -y "${APT_PACKAGES[@]}"
sudo bash -lc "shopt -s nullglob; cp -n /var/cache/apt/archives/*.deb '${APT_DIR}/' || true"

echo "[3/7] Activating virtualenv and validating Python tooling..."
source "${VENV_PATH}/bin/activate"
python3 -m pip install --upgrade pip wheel 'setuptools<82'

echo "[4/7] Ensuring core Python runtime dependencies are installed..."
python3 -m pip install --index-url https://download.pytorch.org/whl/cpu torch
python3 -m pip install \
  'eventlet>=0.40.0' \
  oslo.config \
  oslo.log \
  msgpack \
  tinyrpc \
  flask \
  matplotlib \
  networkx

echo "[5/7] Ensuring Ryu is installed in virtualenv..."
if ! python3 -c "import ryu" >/dev/null 2>&1; then
  if [[ -d "${RYU_SOURCE_DIR}" ]]; then
    python3 -m pip install --no-build-isolation "${RYU_SOURCE_DIR}"
  else
    echo "Ryu source not found at ${RYU_SOURCE_DIR} and ryu is not installed."
    echo "Clone it while online and rerun:"
    echo "  git clone --depth=1 https://github.com/faucetsdn/ryu.git ${RYU_SOURCE_DIR}"
    exit 1
  fi
fi

echo "[6/7] Capturing and downloading Python dependencies for offline use..."
python3 -m pip freeze > "${REQ_RUNTIME}.raw"
if [[ -d "${RYU_SOURCE_DIR}" ]]; then
  grep -viE '^(ryu(@|==)|torch==)' "${REQ_RUNTIME}.raw" > "${REQ_DOWNLOAD}"
else
  grep -viE '^(torch==)' "${REQ_RUNTIME}.raw" > "${REQ_DOWNLOAD}"
fi
grep -viE '^(ryu(@|==)|torch==)' "${REQ_RUNTIME}.raw" > "${REQ_RUNTIME}"
RYU_VERSION="$(python3 -m pip show ryu 2>/dev/null | awk '/^Version:/{print $2}' || true)"
if [[ -n "${RYU_VERSION}" ]]; then
  echo "ryu==${RYU_VERSION}" >> "${REQ_RUNTIME}"
fi
TORCH_VERSION="$(python3 -m pip show torch 2>/dev/null | awk '/^Version:/{print $2}' || true)"
if [[ -n "${TORCH_VERSION}" ]]; then
  echo "torch==${TORCH_VERSION}" >> "${REQ_RUNTIME}"
fi
python3 -m pip download --dest "${PIP_DIR}" -r "${REQ_DOWNLOAD}"
if [[ -n "${TORCH_VERSION}" ]]; then
  python3 -m pip download \
    --dest "${PIP_DIR}" \
    --index-url https://download.pytorch.org/whl/cpu \
    "torch==${TORCH_VERSION}"
fi

if [[ -d "${RYU_SOURCE_DIR}" ]]; then
  python3 -m pip wheel --no-build-isolation --no-deps \
    --wheel-dir "${PIP_DIR}" "${RYU_SOURCE_DIR}"
fi

echo "[7/7] Writing offline install helper..."
cp "${SCRIPT_DIR}/install_offline_bundle.sh" "${BUNDLE_DIR}/install_offline_bundle.sh"
chmod +x "${BUNDLE_DIR}/install_offline_bundle.sh"
cat > "${BUNDLE_DIR}/INSTALL_OFFLINE.md" <<EOF
# Offline Install Guide

Bundle location: ${BUNDLE_DIR}

## Recommended: run the bundled installer
\`\`\`bash
cd ${BUNDLE_DIR}
./install_offline_bundle.sh ${BUNDLE_DIR}
\`\`\`

## 1) Install apt packages offline
\`\`\`bash
sudo cp -f ${APT_DIR}/*.deb /var/cache/apt/archives/
sudo dpkg -i ${APT_DIR}/*.deb || sudo apt-get --no-download -f install -y
\`\`\`

## 2) Install Python packages offline
\`\`\`bash
if [[ ! -f ${VENV_PATH}/bin/activate ]]; then python3 -m venv ${VENV_PATH}; fi
source ${VENV_PATH}/bin/activate
python3 -m pip install --no-index --find-links ${PIP_DIR} -r ${REQ_RUNTIME}
\`\`\`

## 3) Verify SDN tools
\`\`\`bash
ryu-manager --version
iperf3 --version
sudo mn --version
\`\`\`
EOF

rm -f "${REQ_RUNTIME}.raw"

echo "Offline bundle ready at: ${BUNDLE_DIR}"
echo "Apt package files : $(find "${APT_DIR}" -maxdepth 1 -name '*.deb' | wc -l)"
echo "Python artifacts  : $(find "${PIP_DIR}" -maxdepth 1 | wc -l)"
