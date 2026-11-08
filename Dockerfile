# ── Stage 1: build Lambda packages ───────────────────────────────────────────
# sam build runs pip install for each Lambda's requirements.txt natively.
# No --use-container needed: Python 3.12 here matches the Lambda runtime.
FROM python:3.12-slim AS builder

RUN pip install --no-cache-dir aws-sam-cli-local

WORKDIR /app
COPY template.yaml .
COPY lambdas/ lambdas/

RUN sam build --no-cached

# ── Stage 2: deploy image ─────────────────────────────────────────────────────
# Minimal image that ships the pre-built Lambda ZIPs and runs samlocal deploy.
FROM python:3.12-slim

RUN pip install --no-cache-dir aws-sam-cli-local

WORKDIR /app
COPY --from=builder /app/.aws-sam/build /app/.aws-sam/build
COPY --from=builder /app/.aws-sam/build.toml /app/.aws-sam/build.toml
COPY template.yaml .
COPY deploy.sh .
RUN chmod +x deploy.sh

CMD ["./deploy.sh"]
