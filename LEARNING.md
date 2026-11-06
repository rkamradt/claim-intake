# LEARNING.md — Serverless on AWS (via LocalStack)

A running explainer written for an experienced engineer who knows Kubernetes and
backend services but is new to AWS serverless. Each PR adds a section.

---

## PR 1 — SAM skeleton and the health endpoint

### What is AWS SAM?

AWS SAM (Serverless Application Model) is a CloudFormation extension — a thin DSL
that lets you declare Lambda functions, API Gateway routes, SQS queues, DynamoDB
tables, and EventBridge rules in a single `template.yaml`. The `sam build` command
packages your Lambda code into a ZIP (or container image), and `sam deploy` calls
CloudFormation to create or update the stack.

The `samlocal` / `awslocal` wrappers are drop-in replacements that point every AWS
SDK and CLI call at `localhost:4566` instead of the real AWS endpoints. That is the
only change needed to run the same stack on LocalStack.

**The mental model:** SAM is to Lambda what Helm is to Kubernetes. The template is
the chart; `sam deploy` is `helm upgrade --install`; CloudFormation is the control
plane that reconciles desired state.

### AWS::Serverless::Api

```yaml
ClaimIntakeApi:
  Type: AWS::Serverless::Api
  Properties:
    StageName: v1
```

An API Gateway REST API. A "stage" is a named snapshot of the API config — think of
it as a path prefix (`/v1/health`). In production you might have `dev`, `staging`,
`v1`; here we use a single stage.

Terraform equivalent: `aws_api_gateway_rest_api` + `aws_api_gateway_deployment` +
`aws_api_gateway_stage`.

### AWS::Serverless::Function

```yaml
HealthFunction:
  Type: AWS::Serverless::Function
  Properties:
    Handler: handler.lambda_handler
    CodeUri: lambdas/health/
    Events:
      HealthApi:
        Type: Api
        Properties:
          RestApiId: !Ref ClaimIntakeApi
          Path: /health
          Method: GET
```

Three things in one resource:

1. **The function itself** — runtime, memory, timeout, environment variables.
2. **The code location** — `CodeUri` points SAM at the directory to ZIP.
3. **The event source** — `Events` wires an API Gateway route to this function. SAM
   creates the route, the integration, and the Lambda permission in one shot.

The Lambda handler signature is always `(event, context) -> dict`. For API Gateway
the return value must have `statusCode`, `headers`, and `body` (body must be a
string).

### Why a separate Lambda per route?

You could put all routes in one Lambda and dispatch internally (the "monolambda"
pattern). That is simpler to start but couples unrelated paths — a cold start on
`/health` now pays for every import your upload logic needs. The function-per-route
pattern keeps cold starts isolated and lets each function scale independently.
For a learning project, the separation also makes each PR's scope obvious.

### What is LocalStack?

LocalStack runs the AWS service APIs locally. It is not a simulator — it uses the
same wire protocol (HTTP + JSON/XML) as real AWS. Your code and the AWS SDK do not
know the difference; only the endpoint URL changes (`http://localhost:4566` instead
of `https://s3.amazonaws.com`).

The hobby plan (Auth Token required) unlocks persistence and advanced services
(CloudFormation, API Gateway v2, EventBridge). Without it you get a subset of
services and state resets on restart.

### How CI uses LocalStack

The CI workflow starts LocalStack as a GitHub Actions service container — a
sidecar that runs for the life of the job, reachable at `localhost:4566`. After
LocalStack is healthy, `samlocal deploy` creates the stack inside it. The whole
environment is ephemeral: it spins up per PR, runs tests, and is discarded.
Nothing persists between runs; every run starts from a clean slate. This is the
key advantage of throwaway environments for CI.

### The AI gate

Every push runs `gate.sh` from the shared `ci-workflows` repo before any build.
It checks the diff for hardcoded credentials (gitleaks), lints any changed
Kubernetes YAML (kube-linter), and runs a Claude-based review against a rules
file. A commit trailer `AI-Gate-Override: <reason>` bypasses it when needed.
The gate cannot be skipped accidentally — it is a blocking job that all other
jobs `needs`.

---

*More sections added in subsequent PRs:*
- *PR 2: S3 presigned URLs, event notifications, the extract Lambda*
- *PR 3: SQS, event source mappings, DynamoDB, idempotency*
- *PR 4: Dead-letter queues, redrive policy, EventBridge schedules*
- *PR 5: ArgoCD PostSync hooks, in-cluster SAM deployment*
