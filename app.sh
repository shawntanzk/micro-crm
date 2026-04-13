#!/usr/bin/env bash
# Domino App launcher — Domino expects the app to listen on port 8888.
# The CRM_DATA_DIR env var points to persisted storage so data survives
# workspace restarts.  Set it in your Domino project environment variables.
#
# Local usage: bash app.sh
#   Data is stored in ./crm_data by default when not running in Domino.

if [ -n "$DOMINO_PROJECT_NAME" ]; then
  # Running inside Domino — use persisted dataset storage.
  export CRM_DATA_DIR="${CRM_DATA_DIR:-/domino/datasets/local/crm_data}"
  mkdir -p "$CRM_DATA_DIR"
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
  exec streamlit run app.py \
    --server.port 8501 \
    --server.address 0.0.0.0 \
    --server.headless true
fi
