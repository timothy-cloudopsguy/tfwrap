# terraform-bootstrap-example

This repository is an opinionated example showing how to bootstrap Terraform state and deploy a blue/green frontend behind CloudFront using a per-repository S3 backend (S3 is used for both state and locking). The included `tfwrapper` script automates the bootstrapping, state migration, and common `terraform` workflows so you can initialize and apply the example infrastructure quickly.

![demo recording](./render1757429656886.gif)

Quickstart & usage (tfwrapper)
- `tfwrapper` provides a small CLI to bootstrap and manage Terraform for this repo. It supports `bootstrap`, `init`, `plan`, and `apply` commands and accepts an environment selection either via the `ENV` environment variable or the `-e` / `--env` flag.
- Important behavior to know up-front:
  - `tfwrapper` is idempotent: it will check for an SSM Parameter containing backend metadata before bootstrapping resources. If the SSM parameter exists, the script uses it; otherwise it runs the bootstrap and writes the backend metadata to SSM for future runs.
  - The bootstrap and main run both use the same environment selection (`ENV` or `-e`) to determine which `properties.<ENV>.json` file to load.

Usage examples:

```bash
# One-off invocation using environment variable (no export)
ENV=dev ./tfwrapper plan

# One-off invocation using the -e flag
./tfwrapper -e dev plan

# Or export then run multiple commands
export ENV=prod
./tfwrapper plan
./tfwrapper apply
```

# Note
- The chosen environment must match one of the `properties.<ENV>.json` files in the repo (e.g. `dev`, `prod`). `tfwrapper` will fail if it cannot find the matching properties file and `--app-name` is not provided.

Stacks in this example
- This repo now contains two logical stacks:
  - **core**: foundational shared resources for the environment (bootstrap backend, a global CloudFront distribution used for routing/control-plane, CloudFront Function, and a Key-Value Store (KVS)). The `core` stack publishes a small set of values (for example an SSL certificate ARN and backend metadata) into SSM so downstream stacks can consume them.
  - **frontend**: the blue/green frontend stack that creates `blue` and `green` S3 buckets and per-site CloudFront distributions for the application (these are the caching/data-plane distributions). The frontend reads values (example: SSL cert ARN) from SSM and from Terraform outputs when needed.

Ordering and dependencies
- Ordering matters: create and apply the **core** stack before the **frontend** stack. The frontend demonstrates how to consume a value (an SSL certificate ARN) that the core stack publishes into SSM. While the frontend does not itself provision the cert in this example, it expects the cert value to exist in SSM so it can be referenced (this models a real-world pattern where core teams provide common infrastructure and certs).
- Typical flow:
  1. Run `tfwrapper bootstrap` / `tfwrapper apply` for the `core` stack to ensure backend, KVS, CloudFront Function, and SSM values exist.
  2. Run `tfwrapper` for the `frontend` stack (plan/apply) once `core` is present.

Why this repo exists
- **Safe, isolated Terraform state**: The bootstrap creates an S3 bucket scoped to this repository/stack and uses S3 for both state storage and locking so the remote backend is coupled to this repo only (not a shared/global bucket). That reduces blast radius if state becomes corrupted. This is a much simpler solution.
- **Example blue/green frontend**: The Terraform code shows a blue/green deployment pattern for a static/frontend application served via CloudFront and S3.

How the bootstrap works (high level)
- Run `./tfwrapper` from the repo root. The script:
  - Reads the `ENV` environment variable or `-e/--env` flag to determine which `properties.<ENV>.json` file to use; the bootstrap step uses the same environment selection so the same environment/region/account locking applies during bootstrap.
  - Runs a small bootstrap Terraform workspace (in `bootstrap/`) that creates an S3 bucket used for Terraform remote state and locking.
  - Migrates local state into that newly-created S3 backend and then runs the normal plan/apply flow for the example stack.
  - Ensures `backend.tf` is created/managed safely during the migration to avoid accidentally clobbering other backends.

Environment properties (`properties.<ENV>.json`)
- This repo includes `properties.dev.json`, `properties.prod.json` (and the Terraform code reads `properties.${var.environment}.json`). These files lock a deployment to a specific environment, region, and account by providing the canonical properties the stack uses at plan/apply time.
- Why they exist:
  - They capture environment-specific inputs (for example, `app_name`, region-specific settings, or deploy-time flags) in a single JSON file that Terraform and `tfwrapper` read.
  - They allow running the exact same stack multiple times in the same account (e.g., two separate deployments of the same application) by giving each deployment its own properties file and therefore its own derived names and backend keys.
- How to use them:
  - Pass the environment to Terraform (the repository's `variables.tf` expects an `environment` variable that maps to `properties.<env>.json`).
  - `tfwrapper` reads `properties.${ENV}.json` (via `ENV` or `-e/--env`) to synthesize `APP_NAME` and other defaults if not explicitly supplied.

Per-repo backend vs a global backend (and CloudFormation comparison)
- **Per-repo backend (this repo)**
  - Pros: Isolation — state corruption or accidental deletes are limited to this stack. Easier to reason about ownership and lifecycle per project. You can still export values using Terraform outputs and publish important values to SSM Parameter Store for cross-stack consumption.
  - Cons: More S3 resources to manage (one per repo/stack).
- **Global backend (single S3 for many stacks)**
  - Pros: Fewer backend resources to manage centrally; easier to standardize configuration.
  - Cons: Higher blast radius — corruption or misconfiguration could affect many stacks at once.
- **CloudFormation model**
  - CloudFormation manages state for you inside AWS (no separate state bucket). That simplifies things operationally but means you are relying on the service’s control plane; it’s harder to get the same per-stack storage isolation semantics for non-CloudFormation tooling. The per-repo Terraform backend gives you explicit control over state isolation similar to how CloudFormation scopes stacks — but with Terraform you choose where that state lives.

Idempotent bootstrap & SSM-stored backend metadata
- `tfwrapper` is written to be idempotent: it checks for existing backend configuration and the SSM parameter the bootstrap creates before attempting to create backend resources.
- The bootstrap step will create an SSM Parameter that stores the backend content (the backend HCL or equivalent metadata). On subsequent runs `tfwrapper` will read that SSM parameter and reuse the stored backend configuration instead of re-bootstrapping.
  - If the SSM parameter exists and contains valid backend information, the script uses it directly.
  - If the SSM parameter does not exist (first run or intentionally removed), the script runs the bootstrap Terraform under `bootstrap/`, creates the S3 bucket, and then writes the backend content into the SSM parameter for future runs.
- This pattern provides a safe, repeatable bootstrap that avoids recreating or clobbering backend resources once they exist, and makes the script safe to re-run in CI/CD or local workflows.

Sharing values between stacks
- Use `terraform output` to expose ARNs, bucket names, and other values from a stack.
- Persist important, shared values in SSM Parameter Store (the example modules write SSM parameters for bucket names/ARNs). Other stacks can read those parameters to build cross-stack references without directly reading another stack’s state.

Blue/green example notes
- This code models a blue/green deployment for a frontend application behind CloudFront, with separate `blue` and `green` site buckets and CloudFront distributions.
- A higher-level routing layer (included in this repo as "core") is responsible for directing traffic to the blue or green variant. That routing can be implemented by:
  - CloudFront key-value store / request routing (CloudFront Functions or Lambda@Edge decision logic), or
  - A top-level CloudFront + Route53 setup that uses TXT records or other control-plane signals to act as a decision tree (the top-level CloudFront performs routing decisions but does not perform caching for this purpose).

Notes & caveats
- `tfwrapper` contains helpful logic around creating a minimal `backend.tf` and migrating state; read the script if you need to adjust naming, region, or table names (there is an override for the S3 locking configuration if required).
- The bootstrap creates and/or reuses an SSM Parameter containing backend metadata — the script checks this parameter on each run so the bootstrap is safe to run repeatedly.
- If you need cross-stack references, prefer SSM Parameter Store or explicit exported values rather than directly coupling into another stack's state file.

License
- This repository is provided under the license in `LICENSE`.
