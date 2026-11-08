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

### LocalStack CI gotchas (lessons from getting PR 1 green)

**CloudFormation `!Ref` in `Globals` resolves at deploy time.** You can't put
`!Ref SomeResource` in `Globals.Function.Environment` if that resource doesn't
exist in the template yet. CloudFormation validates the entire dependency graph
before creating anything. `Globals` can only reference `Parameters`.

**Lambda needs Docker — even on LocalStack.** By default LocalStack spawns a
Docker container per Lambda invocation. In CI, the standard service-container
approach can't mount the host's Docker socket (`/var/run/docker.sock`). The fix
is to use the official `LocalStack/setup-localstack` GitHub Action, which starts
LocalStack as a regular Docker step (with socket access) rather than a service
container sidecar.

**`AWS_ENDPOINT_URL` must be set explicitly.** The `setup-localstack` action
documents that it exports `AWS_ENDPOINT_URL`, but the value doesn't reach
subsequent job steps reliably. Write it to `$GITHUB_ENV` yourself. The AWS CLI
v2 reads this variable and routes all calls to LocalStack instead of real AWS —
no `awslocal` wrapper needed.

**Don't double-install `awslocal`.** `aws-sam-cli-local` pulls in `awscli-local`
as a dependency, which is an older package that crashes on `import botocore.vendored`
(removed in modern botocore). If the action installs `awslocal`, don't also install
it via pip.

**API Gateway URL in LocalStack is path-based, not domain-based.** The SAM
`Outputs` section emits `https://<id>.execute-api.<region>.amazonaws.com/<stage>`,
which is unreachable in LocalStack. The working URL is:
`http://localhost:4566/restapis/<id>/<stage>/_user_request_/<path>`.
Retrieve the API ID via `aws cloudformation describe-stack-resource --logical-resource-id ClaimIntakeApi`,
not by querying API Gateway by name (SAM may set a different name than you expect).

---

---

## PR 2 — S3, presigned URLs, and the extract Lambda

### AWS::S3::Bucket and event notifications

An S3 bucket is just object storage with a REST API. What makes it interesting
for event-driven architectures is `NotificationConfiguration`: the bucket can
push an event to a Lambda, an SQS queue, or an SNS topic when objects are
created, deleted, or tagged.

The event payload the Lambda receives includes the bucket name, the object key,
the ETag (an MD5 of the object body for single-part uploads), the object size,
and the event timestamp. It does NOT include the object body — the Lambda must
call `s3.get_object()` to fetch it.

### Presigned URLs

A presigned URL is a time-limited, pre-authenticated URL for a single S3
operation (GET or PUT). The Lambda generates it using its IAM credentials; the
client uses it directly without any AWS credentials of its own.

Why use them here: API Gateway has a 10 MB request body limit. By returning a
presigned PUT URL, the upload Lambda sidesteps this limit — the client writes
directly to S3 without touching API Gateway or Lambda memory. The Lambda only
handles the small JSON payload (`{"filename": "claim.txt"}`).

```python
url = s3.generate_presigned_url(
    "put_object",
    Params={"Bucket": BUCKET, "Key": s3_key},
    ExpiresIn=300,  # seconds
)
```

The expiry clock starts when the URL is generated, not when the client starts
uploading. 300 seconds (5 min) is generous for a document upload.

### The S3 ↔ Lambda circular dependency

CloudFormation builds a dependency graph before creating anything. If a Lambda
references an S3 bucket (`!Ref UploadBucket` in env vars or policies), and that
same bucket has a `NotificationConfiguration` pointing at the Lambda, CloudFormation
sees: Lambda → Bucket → Lambda. That's a cycle; deployment fails.

**SAM's `Type: S3` event source makes this worse** — it implicitly injects the
`NotificationConfiguration` into the bucket resource, so the cycle appears even
if you don't write it explicitly.

The fix is the standard CloudFormation pattern:

1. Remove all `!Ref Bucket` from the Lambda (use the literal bucket name string).
2. Create `AWS::Lambda::Permission` explicitly (grants S3 the right to invoke the Lambda).
3. Put `NotificationConfiguration` directly in the bucket resource (not via SAM events).
4. Add `DependsOn: <LambdaPermission>` to the bucket so the permission exists before S3 fires.

Dependency order: `ExtractFunction` → `ExtractFunctionS3Permission` → `UploadBucket`.
No cycles.

### Idempotency key design

The idempotency key is `sha256(s3_key:etag)[:32]`. This is set up in PR 2 (the
extract Lambda computes it) even though it isn't persisted until PR 3 (DynamoDB).

**Why this design:**
- `s3_key` includes a UUID (`uploads/<uuid>/filename`), so two uploads of the
  same file get different keys — they might be legitimately separate claims.
- `etag` is a content hash of the object bytes. If the same client retries an
  upload that succeeded (same bytes, same UUID because they kept the s3_key),
  `etag` matches and the key is identical — the second write to DynamoDB is a
  no-op (conditional put). That's the retry-safe path.
- Together: different documents → different keys; same document re-uploaded →
  same key → one record. This is the definition of idempotency.

**Why this comes up in interviews:** Distributed systems can deliver events more
than once (SQS standard queues have at-least-once delivery). Without idempotency,
a retried SQS message creates a duplicate DynamoDB record. With the conditional
put (`ConditionExpression: attribute_not_exists(pk)`), the second write simply
fails with `ConditionalCheckFailedException`, which the Lambda ignores. One record,
regardless of how many times the message is delivered.

---

## PR 3 — SQS, event source mappings, DynamoDB, and idempotency

### Why SQS sits between extract and verify

The extract Lambda returns as soon as it puts a message on the queue. The verify
Lambda runs independently. This decoupling has three consequences worth knowing:

1. **Retry is free.** If verify crashes, SQS holds the message and retries it
   up to the configured receive count. No data is lost. Without the queue, a
   verify crash would lose the extracted data forever.
2. **Back-pressure.** If verify is slow, messages queue up. SQS can scale the
   verify Lambda concurrency automatically. Without the queue, a slow verify
   would block extract from returning.
3. **Observability.** You can see the queue depth in CloudWatch/LocalStack.
   A growing queue is an early warning that verify is falling behind.

### SQS event source mapping

```yaml
Events:
  SQSExtract:
    Type: SQS
    Properties:
      Queue: !GetAtt ExtractQueue.Arn
      BatchSize: 1
      FunctionResponseTypes:
        - ReportBatchItemFailures
```

SAM creates an `AWS::Lambda::EventSourceMapping` that polls the queue and invokes
the Lambda. `BatchSize: 1` means one SQS message per Lambda invocation (simpler
error handling). `ReportBatchItemFailures` lets the Lambda return a list of
failed message IDs — SQS retries only those, not the whole batch. Without it,
any exception in a batch causes all messages to be retried, including ones that
succeeded.

`VisibilityTimeout` on the queue must be `>= Lambda timeout`. When SQS delivers a
message it hides it for `VisibilityTimeout` seconds. If the Lambda doesn't
finish in time, SQS re-delivers the message. Setting the timeout shorter than the
Lambda runtime means every invocation triggers an unnecessary retry.

### DynamoDB basics

DynamoDB is a key-value / document store. Each item has a primary key; everything
else is optional. `BillingMode: PAY_PER_REQUEST` means you pay per read/write
unit, no capacity planning needed — appropriate for a learning project with
unpredictable traffic.

### Idempotency with conditional writes

The verify Lambda writes each claim with:

```python
table.put_item(
    Item=item,
    ConditionExpression=Attr("pk").not_exists(),
)
```

`attribute_not_exists(pk)` succeeds only when no item with this `pk` exists yet.
On a retry (same `pk`), DynamoDB raises `ConditionalCheckFailedException`. We
catch it and return without error — the original record is preserved.

**Why this matters:** SQS standard queues guarantee *at-least-once* delivery.
Duplicate delivery is not a bug; it is a documented behaviour. The correct
response is to make your consumer idempotent, not to assume duplicates won't
happen. The conditional put is the standard DynamoDB pattern for this.

**The idempotency key** is `sha256(s3_key:etag)[:32]`:
- `s3_key` includes a UUID, so two separate uploads (even of identical content)
  get different keys — they are logically different events.
- `etag` is a content hash. If the *same upload* (same s3_key, same bytes) is
  processed twice because SQS re-delivered the message, `etag` is the same, so
  the key is the same, and the second `put_item` is a no-op.

### ReportBatchItemFailures vs a simple exception

Without `ReportBatchItemFailures`, if the Lambda raises an exception, SQS retries
the *entire batch*. With `BatchSize: 1` this doesn't matter much — there is only
one message per invocation. But it matters as soon as you increase `BatchSize`
for throughput: a single bad message would cause all good messages in the batch
to be retried. `ReportBatchItemFailures` lets you return exactly which message
IDs failed; the rest are acknowledged.

The verify Lambda distinguishes two kinds of "failure":
1. **Verification failure** (bad total, missing field): the claim is stored with
   `status=needs_review`. This is *not* a processing error — the message is
   acknowledged normally.
2. **Processing error** (unexpected exception, DynamoDB unavailable): the message
   ID is added to `batchItemFailures`. SQS re-delivers it for a retry.

*More sections added in subsequent PRs:*
- *PR 2: S3 presigned URLs, event notifications, the extract Lambda*
- *PR 3: SQS, event source mappings, DynamoDB, idempotency*
- *PR 4: Dead-letter queues, redrive policy, EventBridge schedules*
- *PR 5: ArgoCD PostSync hooks, in-cluster SAM deployment*
