#!/bin/bash
# deploy.sh — runs inside the PostSync Job container.
# Waits for LocalStack to be healthy, then deploys the SAM stack.
set -euo pipefail

LOCALSTACK_URL="${AWS_ENDPOINT_URL:-http://localstack.localstack.svc.cluster.local:4566}"
HEALTH_URL="${LOCALSTACK_URL}/_localstack/health"
MAX_WAIT=120  # seconds
WAITED=0

echo "Waiting for LocalStack at ${HEALTH_URL}..."
until curl -sf "${HEALTH_URL}" > /dev/null 2>&1; do
  if [ "${WAITED}" -ge "${MAX_WAIT}" ]; then
    echo "ERROR: LocalStack did not become healthy after ${MAX_WAIT}s" >&2
    exit 1
  fi
  sleep 3
  WAITED=$((WAITED + 3))
done
echo "LocalStack is ready (waited ${WAITED}s)."

samlocal deploy \
  --stack-name claim-intake \
  --resolve-s3 \
  --no-confirm-changeset \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    "AnthropicApiKey=${ANTHROPIC_API_KEY:-mock}" \
    "UseRealAI=${USE_REAL_AI:-false}"

echo "claim-intake stack deployed successfully."
