This Terraform module mirrors key pieces of the existing CDK stacks under `core/cdk` and is designed to be environment-aware by reading `properties.<env>.(json|yaml|yml)` files.

Usage

- Select an environment by passing `-var "environment=dev"` or `-var "environment=prod"` (defaults to `dev`). The module reads `core/properties.<env>.yaml`, and accepts exactly one of `properties.<env>.json`, `properties.<env>.yaml` or `properties.<env>.yml`.
- Example: `terraform init && terraform plan -var "environment=prod" -var "region=us-east-1"`

Notes and differences from CDK

- The CDK uses some higher-level constructs (CloudFront KeyValueStore and Function for routing) that are not yet mapped here; this Terraform module implements the main building blocks: ACM certificate with DNS validation, CloudFront distribution, Route53 records, SES domain identity, S3 logging bucket, and SSM parameters.
- You can extend the module to add CloudFront Functions or other features using `aws_cloudfront_function` and `aws_cloudfront_function_association` as supported by the AWS provider. 