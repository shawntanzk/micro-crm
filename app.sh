#!/usr/bin/env bash
# Domino App launcher — Domino expects the app to listen on port 8888.
# The CRM_DATA_DIR env var points to persisted storage so data survives
# workspace restarts.  Set it in your Domino project environment variables.

export CRM_DATA_DIR="${CRM_DATA_DIR:-/domino/datasets/local/crm_data}"
mkdir -p "$CRM_DATA_DIR"

streamlit run app.py \
  --server.port 8888 \
  --server.address 0.0.0.0 \
  --server.headless true \
  --server.enableCORS false \
  --server.enableXsrfProtection false
