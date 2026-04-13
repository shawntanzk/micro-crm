if [ -n "$DOMINO_PROJECT_NAME" ]; then
  export CRM_DATA_DIR="${CRM_DATA_DIR:-/domino/datasets/local/crm_data}"
  PORT=8888
  COOKIE_FLAGS="--server.enableCORS false \
    --server.enableXsrfProtection false \
    --server.enableWebsocketCompression false \
    --server.cookieSecret 'b5df56e014904ae59c704f75918633686f9067b1075cdfdc9f6510370ddd78a3'"
else

  export CRM_DATA_DIR="${CRM_DATA_DIR:-$(pwd)/crm_data}"
  PORT=8501
  COOKIE_FLAGS=""
fi

mkdir -p "$CRM_DATA_DIR"


streamlit run app.py \
  --server.port "$PORT" \
  --server.address 0.0.0.0 \
  --server.headless true \
  $COOKIE_FLAGS
