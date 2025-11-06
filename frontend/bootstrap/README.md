## What this bootstrap does

This directory contains minimal notes for the *bootstrap* step used by `tfwrap`. Its goal is to ensure the remote Terraform backend and locking resources exist before running the rest of the infrastructure tooling.

- **Primary purpose**: create and configure a remote state backend S3 bucket for state and state lock, the current version does not use KMS keys.
- **When to use**: normally `tfwrap` automates this step. Use the manual instructions below only if `tfwrap` fails, you need to customize resources, or you prefer to create them yourself.

## What tfwrap usually creates

When working correctly, `tfwrap` will create or verify the following (examples for an AWS-based backend):

- **S3 bucket** for remote state (with versioning and server-side encryption) and state locking
- Backend configuration files referencing the above resources

## Manual creation examples

If you need to create the resources manually, here are minimal examples using the AWS CLI and a sample Terraform `backend` block.

> Update the names/region/ids below to match your naming policy and region.

### 1) Create S3 bucket for state

```bash
aws s3api create-bucket \
  --bucket my-terraform-state-bucket \
  --region us-west-2 \
  --create-bucket-configuration LocationConstraint=us-west-2

# enable versioning
aws s3api put-bucket-versioning \
  --bucket my-terraform-state-bucket \
  --versioning-configuration Status=Enabled

# enable default encryption (use the KMS key id if you created one)
aws s3api put-bucket-encryption \
  --bucket my-terraform-state-bucket \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms"}}]}'
```

### 4) Example Terraform backend block

Add this to your root module `backend` configuration (adjust names/region/key/table):

```hcl
terraform {
  backend "s3" {
    bucket         = "my-terraform-state-bucket"
    key            = "global/terraform.tfstate"
    region         = "us-west-2"
    encrypt        = true
    use_lockfile   = true
    kms_key_id     = "arn:aws:kms:us-west-2:123456789012:key/abcd-ef01-2345-..." # optional
  }
}
```

### 5) Permissions and troubleshooting notes

- Ensure the IAM principal running `terraform init` has permissions to read/write the S3 bucket, manage object versions, and access the optional KMS key if `kms_key_id` is used. Typical S3 permissions include `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket`, and `s3:GetBucketVersioning`.
- If `terraform init` fails with locking errors, verify the S3 bucket name, that versioning is enabled, and that the calling credentials have the required S3 permissions. If you're relying on S3 object-based locking, ensure any required S3 Object Lock configuration is set and that your workflow supports it.
- If encryption or KMS errors occur, verify the `kms_key_id` is correct and that the principal has `kms:Encrypt`/`kms:Decrypt`/`kms:GenerateDataKey` as needed.

## When to prefer manual creation

- `tfwrap` can't access the target account or lacks permissions.
- You have strict naming/placement policies and must pre-create resources.
- You want to inspect or harden the resources before allowing automation to manage them.

If you add manual resources, update any `tfwrap` or CI configuration so it uses the same names/IDs.

---

If you want, I can also add a small `terraform` snippet in this directory to create these resources idempotently — tell me which cloud provider and naming conventions you prefer.
