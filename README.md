# tfwrap

A CLI tool for managing Terraform remote backends and bootstrapping infrastructure with AWS S3 state storage.


[![PyPI version](https://badge.fury.io/py/tfwrap.svg)](https://pypi.org/project/tfwrap/)
![Python versions](https://img.shields.io/pypi/pyversions/tfwrap)
![License](https://img.shields.io/github/license/timothy-cloudopsguy/tfwrap)

## Installation

### From PyPI

```bash
pip install tfwrap
```

To use YAML properties files, install the optional extra:

```bash
pip install 'tfwrap[yaml]'
```

### From source

```bash
git clone https://github.com/timothy-cloudopsguy/tfwrap.git
cd tfwrap
pip install .
```

![demo recording](./render1757429656886.gif)

## Quickstart

`tfwrap` provides a CLI to bootstrap and manage Terraform backends. It supports `bootstrap`, `init`, `plan`, `apply`, `destroy`, `destroy-all`, and `clean` commands and accepts an environment selection either via the `ENV` environment variable or the `-e` / `--env` flag.

### Important behavior to know up-front:
- `tfwrap` is idempotent: it will check for an SSM Parameter containing backend metadata before bootstrapping resources. If the SSM parameter exists, the script uses it; otherwise it runs the bootstrap and writes the backend metadata to SSM for future runs.
- The bootstrap and main run both use the same environment selection (`ENV` or `-e`) to determine which `properties.<ENV>.json` / `.yaml` / `.yml` file to load from `--target-dir` (default `.`).

### Usage examples:

```bash
# One-off invocation using environment variable (no export)
ENV=dev tfwrap plan

# One-off invocation using the -e flag
tfwrap -e dev plan

# Or export then run multiple commands
export ENV=prod
tfwrap plan
tfwrap apply
```

# Note
- The chosen environment must match a `properties.<ENV>.json` (or `.yaml` / `.yml`) file in `--target-dir` (e.g. `dev`, `prod`). `tfwrap` will fail if it cannot find the matching properties file and `--app-name` is not provided.

Stacks in this example
- This repo now contains two logical stacks:
  - **core**: foundational shared resources for the environment (bootstrap backend, a global CloudFront distribution used for routing/control-plane, CloudFront Function, and a Key-Value Store (KVS)). The `core` stack publishes a small set of values (for example an SSL certificate ARN and backend metadata) into SSM so downstream stacks can consume them.
  - **frontend**: the blue/green frontend stack that creates `blue` and `green` S3 buckets and per-site CloudFront distributions for the application (these are the caching/data-plane distributions). The frontend reads values (example: SSL cert ARN) from SSM and from Terraform outputs when needed.

Ordering and dependencies
- Ordering matters: create and apply the **core** stack before the **frontend** stack. The frontend demonstrates how to consume a value (an SSL certificate ARN) that the core stack publishes into SSM. While the frontend does not itself provision the cert in this example, it expects the cert value to exist in SSM so it can be referenced (this models a real-world pattern where core teams provide common infrastructure and certs).
- Typical flow:
  1. Run `tfwrap bootstrap` / `tfwrap apply` for the `core` stack to ensure backend, KVS, CloudFront Function, and SSM values exist.
  2. Run `tfwrap` for the `frontend` stack (plan/apply) once `core` is present.

Why this repo exists
- **Safe, isolated Terraform state**: The bootstrap creates an S3 bucket scoped to this repository/stack and uses S3 for both state storage and locking so the remote backend is coupled to this repo only (not a shared/global bucket). That reduces blast radius if state becomes corrupted. This is a much simpler solution.
- **Example blue/green frontend**: The Terraform code shows a blue/green deployment pattern for a static/frontend application served via CloudFront and S3.

How the bootstrap works (high level)
- Run `./tfwrap` from the repo root. The script:
  - Reads the `ENV` environment variable or `-e/--env` flag to determine which `properties.<ENV>` file to use; the bootstrap step uses the same environment selection so the same environment/region/account locking applies during bootstrap.
  - Runs a small bootstrap Terraform workspace (in `bootstrap/`) that creates an S3 bucket used for Terraform remote state and locking.
  - Migrates local state into that newly-created S3 backend and then runs the normal plan/apply flow for the example stack.
  - Ensures `backend.tf` is created/managed safely during the migration to avoid accidentally clobbering other backends.

Environment properties (`properties.<ENV>.json` / `.yaml` / `.yml`)
- This repo includes `properties.dev.json`, `properties.prod.json` (and the Terraform code reads `properties.${var.environment}.json`, `.yaml` or `.yml`). These files lock a deployment to a specific environment, region, and account by providing the canonical properties the stack uses at plan/apply time.
- Why they exist:
  - They capture environment-specific inputs (for example, `app_name`, region-specific settings, or deploy-time flags) in a single JSON or YAML file that Terraform and `tfwrap` read.
  - They allow running the exact same stack multiple times in the same account (e.g., two separate deployments of the same application) by giving each deployment its own properties file and therefore its own derived names and backend keys.
- How to use them:
  - Pass the environment to Terraform (the repository's `variables.tf` expects an `environment` variable that maps to `properties.<env>.<ext>`).
  - `tfwrap` reads `properties.${ENV}.<ext>` (via `ENV` or `-e/--env`) from `--target-dir` (default `.`) to synthesize `APP_NAME` and other defaults if not explicitly supplied. It never falls back to the current directory. `--app-name` skips the lookup entirely.
- Formats and precedence:
  - `.json`, `.yaml` and `.yml` are all accepted. YAML needs PyYAML: `pip install 'tfwrap[yaml]'` (JSON-only users don't need it).
  - If more than one exists, `tfwrap` and Terraform pick `json` > `yaml` > `yml`; `tfwrap` logs a WARNING naming the file used and the ones ignored.
  - Fail-fast: if the selected file can't be parsed, is empty, or its top level isn't a mapping, `tfwrap` exits with code 2 and prints the path and parser error. It does not fall back to another file.
  - Quote YAML strings. Unquoted values like `yes`, `no`, `on` or `0123` are read as booleans or numbers by both tfwrap and Terraform (e.g. `on` becomes `true`, `0123` becomes `123`).
- Example:

```yaml
# core/properties.dev.yaml
app_name: "terraformExampleCore"
route53:
  domain_name: "dev.verihire.cc"
  ttl: 60
```

```bash
tfwrap -e dev --target-dir core plan   # reads core/properties.dev.{json,yaml,yml}
```

Upgrading to 1.0.10
- The properties file is now read from `--target-dir`, not the current directory. If you pass `--target-dir` and kept `properties.<ENV>.json` only in the current directory, move it into `target_dir` (otherwise `tfwrap` exits with code 2). With the default `--target-dir .` nothing changes.
- YAML properties files are supported via `pip install 'tfwrap[yaml]'`.
- Unparsable properties files now fail with exit code 2 instead of silently producing an empty app name.

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
- `tfwrap` is written to be idempotent: it checks for existing backend configuration and the SSM parameter the bootstrap creates before attempting to create backend resources.
- The bootstrap step will create an SSM Parameter that stores the backend content (the backend HCL or equivalent metadata). On subsequent runs `tfwrap` will read that SSM parameter and reuse the stored backend configuration instead of re-bootstrapping.
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
- `tfwrap` contains helpful logic around creating a minimal `backend.tf` and migrating state; read the script if you need to adjust naming, region, or table names (there is an override for the S3 locking configuration if required).
- The bootstrap creates and/or reuses an SSM Parameter containing backend metadata — the script checks this parameter on each run so the bootstrap is safe to run repeatedly.
- If you need cross-stack references, prefer SSM Parameter Store or explicit exported values rather than directly coupling into another stack's state file.

License
- This repository is provided under the license in `LICENSE`.
