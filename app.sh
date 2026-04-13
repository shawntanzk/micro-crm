#!/usr/bin/env bash
# Domino App launcher — Domino expects the app to listen on port 8888.
# The CRM_DATA_DIR env var points to persisted storage so data survives
# workspace restarts.  Set it in your Domino project environment variables.
#
# CRM_COOKIE_SECRET must be set as a Domino environment variable.
# Generate a value once with: python -c "import secrets; print(secrets.token_hex(32))"
# A stable secret prevents 400 errors from OpenResty when the app restarts
# (Streamlit generates a random secret on each start otherwise, invalidating
# all existing browser sessions).

export CRM_DATA_DIR="${CRM_DATA_DIR:-/domino/datasets/local/crm_data}"
mkdir -p "$CRM_DATA_DIR"

if [ -z "$CRM_COOKIE_SECRET" ]; then
  echo "WARNING: CRM_COOKIE_SECRET is not set. Users will see 400 errors after app restarts." >&2
fi

streamlit run app.py \
  --server.port 8888 \
  --server.address 0.0.0.0 \
  --server.headless true \
  --server.enableCORS false \
  --server.enableXsrfProtection false \
  --server.enableWebsocketCompression false \
  --server.cookieSecret "${CRM_COOKIE_SECRET:-insecure-default-change-me}"
