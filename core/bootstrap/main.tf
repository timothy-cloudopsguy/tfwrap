provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

locals {
  props_base = "${path.module}/../properties.${var.environment}"
  props_file = fileexists("${local.props_base}.json") ? "${local.props_base}.json" : (fileexists("${local.props_base}.yaml") ? "${local.props_base}.yaml" : "${local.props_base}.yml")
  props      = endswith(local.props_file, ".json") ? jsondecode(file(local.props_file)) : yamldecode(file(local.props_file))

  # Pretty name (OK for DynamoDB, tags, etc.)
  env_title           = title(var.environment)
  generated_app_name  = local.props.app_name != "" ? "${local.props.app_name}${local.env_title}" : "coreDev"

  # S3-safe slug (lowercase + hyphens)
  app_slug            = lower(local.props.app_name != "" ? "${local.props.app_name}${var.environment}" : "core-dev")

  acct                = replace(data.aws_caller_identity.current.account_id, ".", "")

  # Force lowercase bucket name
  generated_bucket_name = var.bucket_name != "" ? lower(var.bucket_name) : "${local.acct}-${local.app_slug}-tfstate"
}

resource "aws_s3_bucket" "tfstate" {
  bucket = local.generated_bucket_name
  force_destroy = true # Allows deletion of non-empty bucket

  lifecycle {
    prevent_destroy = true
  }
  tags   = merge({ Name = local.generated_bucket_name }, var.tags)
}

resource "aws_s3_bucket_versioning" "tfstate_versioning" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate_sse" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.kms_key_id == "" ? "AES256" : "aws:kms"
      kms_master_key_id = var.kms_key_id == "" ? null : var.kms_key_id
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate_block" {
  bucket = aws_s3_bucket.tfstate.id

  block_public_acls   = true
  block_public_policy = true
  ignore_public_acls  = true
  restrict_public_buckets = true
}

output "generated_bucket_name" { value = local.generated_bucket_name } 
