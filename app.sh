#!/usr/bin/env bash
# Domino App launcher — Domino expects the app to listen on port 8888.
# The CRM_DATA_DIR env var points to persisted storage so data survives
# workspace restarts.  Set it in your Domino project environment variables.

# Strip carriage returns in case the file was copied from Windows (CRLF endings)
CRM_DATA_DIR=$(printf '%s' "${CRM_DATA_DIR:-/domino/datasets/local/crm_data}" | tr -d '\r')
export CRM_DATA_DIR
mkdir -p "$CRM_DATA_DIR"

streamlit run app.py \
  --server.port 8888 \
  --server.address 0.0.0.0 \
  --server.headless true \
  --server.enableCORS false \
  --server.enableXsrfProtection false \
  --server.enableWebsocketCompression false \
  --server.cookieSecret "3f300365abe41c0ab9367ad7e695ceda4b9f24e80986533bcff8ca56fc7f38d1"
