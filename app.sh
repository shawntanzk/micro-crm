#!/usr/bin/env bash
# Domino App launcher — Domino expects the app to listen on port 8888.
# CRM_DATA_DIR must be set as a Domino environment variable pointing to
# persisted storage so data survives workspace restarts.

mkdir -p "$CRM_DATA_DIR"

streamlit run app.py \
  --server.port 8888 \
  --server.address 0.0.0.0 \
  --server.headless true \
  --server.enableCORS false \
  --server.enableXsrfProtection false \
  --server.enableWebsocketCompression false \
  --server.cookieSecret "3f300365abe41c0ab9367ad7e695ceda4b9f24e80986533bcff8ca56fc7f38d1"
