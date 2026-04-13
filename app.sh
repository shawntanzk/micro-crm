#!/usr/bin/env bash
# Launcher for both Domino and local usage.
# Proxy flags (CORS, XSRF, etc.) are in .streamlit/config.toml.
# This script only controls the port and data directory.
set -euo pipefail

if [ -n "${DOMINO_PROJECT_NAME:-}" ]; then
  export CRM_DATA_DIR="${CRM_DATA_DIR:-/domino/datasets/local/crm_data}"
  mkdir -p "$CRM_DATA_DIR"
  exec streamlit run app.py --server.port 8888
else
  export CRM_DATA_DIR="${CRM_DATA_DIR:-$(pwd)/crm_data}"
  mkdir -p "$CRM_DATA_DIR"
  exec streamlit run app.py --server.port 8501
fi
