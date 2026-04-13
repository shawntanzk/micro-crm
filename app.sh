#!/usr/bin/env bash
set -euo pipefail

echo "=== app.sh starting ==="
echo "DOMINO_PROJECT_NAME=${DOMINO_PROJECT_NAME:-<not set>}"
echo "CRM_DATA_DIR=${CRM_DATA_DIR:-<not set>}"
echo "PWD=$(pwd)"
echo "PATH=$PATH"
echo "Python: $(python --version 2>&1 || echo 'not found')"
echo "Streamlit: $(streamlit --version 2>&1 || echo 'not found')"
echo "=== Installing/verifying dependencies ==="
pip install --quiet streamlit

if [ -n "${DOMINO_PROJECT_NAME:-}" ]; then
  # Running inside Domino — use persisted dataset storage.
  export CRM_DATA_DIR="${CRM_DATA_DIR:-/domino/datasets/local/crm_data}"
  mkdir -p "$CRM_DATA_DIR"
  echo "=== Launching in Domino mode, CRM_DATA_DIR=$CRM_DATA_DIR ==="
  exec streamlit run app.py \
    --server.port 8888 \
    --server.address 0.0.0.0 \
    --server.headless true \
    --server.enableCORS false \
    --server.enableXsrfProtection false \
    --server.enableWebsocketCompression false \
    --server.cookieSecret b5df56e014904ae59c704f75918633686f9067b1075cdfdc9f6510370ddd78a3
else
  # Running locally — store data next to the repo, use default Streamlit port.
  export CRM_DATA_DIR="${CRM_DATA_DIR:-$(pwd)/crm_data}"
  mkdir -p "$CRM_DATA_DIR"
  echo "=== Launching in local mode, CRM_DATA_DIR=$CRM_DATA_DIR ==="
  exec streamlit run app.py \
    --server.port 8501 \
    --server.address 0.0.0.0 \
    --server.headless true
fi
