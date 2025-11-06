#!/usr/bin/env bash
set -euo pipefail

# tfwrapper
# Manage terraform remote backend via SSM parameter or by running bootstrap terraform.
# Usage:
#   ./tfwrapper bootstrap [-e ENV] [-r REGION] [--target-dir PATH] [--app-name NAME]
#   ./tfwrapper plan|apply|destroy|destroy-all [-e ENV] [-r REGION] [--target-dir PATH] [--force] [--force-copy] [--app-name NAME]
#
# Behavior:
# - Synthesizes APP_NAME (from properties.${ENV}.json unless --app-name supplied) and ACCOUNT_ID.
# - Looks for SSM parameter at /terraform/${ACCOUNT_ID}-${SAFE_APP_NAME}.
#   - If found: writes its value to backend.tf in the target dir and uses it for terraform init.
#   - If not found: runs bootstrap terraform (in bootstrap/), creates the S3 bucket,
#     creates the SSM parameter with the backend.tf contents, then uses it.

ENV="${ENV:- }"
REGION="${AWS_REGION:-us-east-1}"
BUCKET_OVERRIDE=""
TARGET_DIR_OVERRIDE=""
TARGET_DIR="${TARGET_DIR:-.}"
FORCE_COPY="false"
APP_NAME_OVERRIDE=""
FORCE_DELETE="false"

COMMAND="init"

print_usage() {
  cat <<EOF
Usage:
  $0 bootstrap [-e ENV] [-r REGION] [--target-dir PATH] [--app-name NAME]
  $0 init|plan|apply|destroy|destroy-all [-e ENV] [-r REGION] [--target-dir PATH] [--force] [--force-copy] [--app-name NAME]

Commands:
  bootstrap   Run the bootstrap terraform in ./bootstrap to create remote backend and SSM entry.
  init        Ensure backend exists (via SSM or bootstrap), then run 'terraform init' in target dir.
  plan        Ensure backend exists (via SSM or bootstrap), init, then run 'terraform plan' in target dir.
  apply       Ensure backend exists (via SSM or bootstrap), init, then run 'terraform apply' in target dir.
  destroy     Destroy the top-level stack (target dir) only.
  destroy-all Destroy the top-level stack then destroy the bootstrap S3 bucket.

Options:
  -e, --env           Environment (default: dev)
  -r, --region        AWS region (default: us-east-1)
  --target-dir        Target terraform directory to run init/plan/apply in (default: .)
  --force             Skip interactive confirmation prompts for destructive commands (destroy/destroy-all)
  --force-copy        When migrating local state into newly created backend, pass -force-copy to terraform init
  --app-name          Override app name (otherwise read from properties.${ENV}.json)
  -h, --help          Show this help
EOF
}

# Parse command (first arg may be a command)
if [[ $# -ge 1 ]]; then
  case "$1" in
    bootstrap|init|plan|apply|destroy|destroy-all)
      COMMAND="$1"; shift || true
      ;;
  esac
fi

while [[ $# -gt 0 ]]; do
  key="$1"
  case $key in
    -e|--env)
      ENV="$2"; shift; shift;;
    -r|--region)
      REGION="$2"; shift; shift;;
    --target-dir)
      TARGET_DIR_OVERRIDE="$2"; shift; shift;;
    --force-copy)
      FORCE_COPY="true"; shift;;
    --app-name)
      APP_NAME_OVERRIDE="$2"; shift; shift;;
    --force)
      FORCE_DELETE="true"; shift;;
    -h|--help)
      print_usage; exit 0;;
    *)
      echo "Unknown arg: $1" >&2; print_usage; exit 1;;
  esac
done

# Set target dir
if [[ -n "$TARGET_DIR_OVERRIDE" ]]; then
  TARGET_DIR="$TARGET_DIR_OVERRIDE"
  mkdir -p "$TARGET_DIR"
else
  TARGET_DIR="."
fi

# Helpers
log() { printf '[%s] INFO: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
err() { printf '[%s] ERROR: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; }

# Run a command while logging it. Accepts the command as arguments (preserves arrays).
run_and_log() {
  printf '[%s] CMD: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
  "$@" || { err "Command failed: $*"; return 1; }
}

confirm_prompt() {
  # Usage: confirm_prompt "Are you sure you want to..."
  if [[ "$FORCE_DELETE" == "true" ]]; then
    return 0
  fi
  read -r -p "$1 [y/N]: " yn
  case "$yn" in
    [Yy]*) return 0;;
    *) return 1;;
  esac
}

synthesize_app_name_and_account() {
  log "synthesize_app_name_and_account: BEGIN"
  if [[ -n "$APP_NAME_OVERRIDE" ]]; then
    APP_NAME="$APP_NAME_OVERRIDE"
  else
    if [[ -f "properties.${ENV}.json" ]]; then
      APP_NAME=$(jq -r '.app_name' "properties.${ENV}.json")
    else
      APP_NAME=""
    fi
  fi

  if [[ -z "$APP_NAME" || "$APP_NAME" == "null" ]]; then
    err "Unable to determine app name. Ensure properties.${ENV}.json exists and contains an 'app_name' field, or provide --app-name."
    exit 2
  fi

  SAFE_APP_NAME=$(echo "${APP_NAME}${ENV}" | tr '[:upper:]' '[:lower:]')

  ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region "$REGION")
  if [[ -z "$ACCOUNT_ID" ]]; then
    err "Unable to determine AWS account id. Ensure AWS CLI is configured."
    exit 2
  fi

  SSM_PARAM_NAME="/terraform/backend/${ACCOUNT_ID}-${SAFE_APP_NAME}"
}

get_ssm_backend() {
  # Returns the parameter value or empty string if not found
  if aws ssm get-parameter --name "$SSM_PARAM_NAME" --with-decryption --region "$REGION" >/dev/null 2>&1; then
    aws ssm get-parameter --name "$SSM_PARAM_NAME" --with-decryption --query Parameter.Value --output text --region "$REGION"
  else
    echo ""
  fi
}

put_ssm_backend() {
  local value="$1"
  aws ssm put-parameter --name "$SSM_PARAM_NAME" --value "$value" --type String --overwrite --region "$REGION" >/dev/null
}

delete_ssm_backend() {
  log "delete_ssm_backend: BEGIN"
  if aws ssm delete-parameter --name "$SSM_PARAM_NAME" --region "$REGION" >/dev/null 2>&1; then
    log "Deleted SSM parameter $SSM_PARAM_NAME"
  else
    log "SSM parameter $SSM_PARAM_NAME not found or could not be deleted"
  fi
}

write_backend_hcl_to_file() {
  log "write_backend_hcl_to_file: BEGIN"
  local content="$1"
  # Write as a proper Terraform file so `terraform init` reads the backend block
  local out_path="$TARGET_DIR/backend.tf"
  echo "$content" > "$out_path"
  log "Wrote $out_path"
}

write_local_backend_tf() {
  log "write_local_backend_tf: BEGIN"
  local dir="$1"
  local backend_tf_path="$dir/backend.tf"
  cat > "$backend_tf_path" <<'TFEOF'
terraform {
  backend "local" {
    path = "bootstrap.tfstate"
  }
}
TFEOF
  log "Wrote local backend.tf at $backend_tf_path"
}

build_backend_content() {
  # Arguments: bucket region account
  local bucket="$1"; local region="$2"; local account="$3"
  cat <<EOF
terraform {
  backend "s3" {
    bucket = "${bucket}"
    key    = "terraform.${account}-${region}-${SAFE_APP_NAME}.tfstate"
    region = "${region}"
    encrypt = true
    use_lockfile = true
  }
}
EOF
}

erase_backend_tf() {
  log "erase_backend_tf: BEGIN"
  local dir="$1"
  local backend_tf_path="$dir/backend.tf"
  if [[ -f "$backend_tf_path" ]]; then
    rm "$backend_tf_path"
  fi
  log "Erased backend.tf at $backend_tf_path"
}

# Helper: empty an S3 bucket (handles versioned and non-versioned objects)
empty_s3_bucket() {
  log "empty_s3_bucket: BEGIN"
  local bucket="$1"
  local region="${2:-$REGION}"

  if [[ -z "$bucket" ]]; then
    log "No bucket name provided; skipping empty_s3_bucket"
    return 0
  fi

  log "Emptying S3 bucket $bucket in region $region"

  # Try the simple recursive remove (handles most non-versioned buckets)
  if aws s3 rm "s3://${bucket}" --recursive --region "$region" >/dev/null 2>&1; then
    log "Removed non-versioned objects from s3://$bucket"
  else
    log "aws s3 rm recursive returned non-zero or removed nothing; continuing to remove versions/delete markers if present"
  fi

  # Remove versioned objects and delete markers in batches using list-object-versions
  while true; do
    local list_json
    list_json=$(aws s3api list-object-versions --bucket "$bucket" --region "$region" --output json 2>/dev/null) || list_json=""
    if [[ -z "$list_json" ]]; then
      # No versions or failed to list; break out
      break
    fi

    local delete_json
    delete_json=$(echo "$list_json" | jq -c '{Objects: ([.Versions[]? | {Key:.Key,VersionId:.VersionId}] + [.DeleteMarkers[]? | {Key:.Key,VersionId:.VersionId}]), Quiet: false}')
    local count
    count=$(echo "$delete_json" | jq '.Objects | length')
    if [[ "$count" -eq 0 ]]; then
      break
    fi

    aws s3api delete-objects --bucket "$bucket" --region "$region" --delete "$delete_json" >/dev/null || { err "Failed to delete object versions from s3://$bucket"; return 1; }
    # loop to ensure all versions/delete markers removed
  done

  log "empty_s3_bucket: DONE for $bucket"
}

ensure_minimal_backend_tf() {
  log "ensure_minimal_backend_tf: BEGIN"
  local dir="$1"
  local backend_tf_path="$dir/backend.tf"
  if [[ ! -f "$backend_tf_path" ]]; then
    cat > "$backend_tf_path" <<'TFEOF'
terraform {
  backend "s3" {}
}
TFEOF
    log "Created minimal backend.tf at $backend_tf_path"
  else
    log "Found existing backend.tf at $backend_tf_path"
  fi
}

run_terraform_init_with_backend_file() {
  log "run_terraform_init_with_backend_file: BEGIN"
  pushd "$TARGET_DIR" >/dev/null
  # backend config is written as a proper Terraform file (`backend.tf`), so just reconfigure
  INIT_CMD=(terraform init -reconfigure -input=false)
  if [[ "$FORCE_COPY" == "true" ]]; then
    INIT_CMD+=( -force-copy )
  fi
  run_and_log "${INIT_CMD[@]}" || { popd >/dev/null; exit 1; }
  popd >/dev/null
}

run_bootstrap_and_create_ssm() {
  log "run_bootstrap_and_create_ssm: BEGIN"
  # Find bootstrap dir
  BOOTSTRAP_LOCATIONS=("${TARGET_DIR}/bootstrap" "bootstrap")
  local found=""
  for dir in "${BOOTSTRAP_LOCATIONS[@]}"; do
    if [[ -d "$dir" ]]; then
      found="$dir"
      break
    fi
  done
  if [[ -z "$found" ]]; then
    err "Bootstrap directory not found in ${BOOTSTRAP_LOCATIONS[*]}. Cannot bootstrap."
    exit 1
  fi

  log "Found bootstrap directory at $found. Running terraform init and apply..."
  erase_backend_tf "$found"

  pushd "$found" >/dev/null

  run_and_log terraform init -input=false -reconfigure || { err "terraform init failed in $found"; popd >/dev/null; exit 1; }

  run_and_log terraform apply -auto-approve -input=false -var "environment=${ENV}" -var "region=${REGION}" || { err "terraform apply failed in $found"; popd >/dev/null; exit 1; }

  log "Bootstrap terraform apply completed in $found."

  # The bootstrap terraform is expected to output or create the S3 bucket name
  # We allow overrides via env variables if present
  if [[ -n "$BUCKET_OVERRIDE" ]]; then
    BUCKET_NAME="$BUCKET_OVERRIDE"
  else
    # attempt to read bucket name from terraform output if available
    if terraform output -json >/dev/null 2>&1; then
      if terraform output -json | jq -r 'select(has("bucket_name")) .bucket_name.value' >/dev/null 2>&1; then
        BUCKET_NAME=$(terraform output -json | jq -r '.bucket_name.value')
      fi
    fi
    BUCKET_NAME="${BUCKET_NAME:-${ACCOUNT_ID}-${SAFE_APP_NAME}-tfstate}"
  fi

  # Build backend.hcl content for the main target and store into SSM
  backend_content=$(build_backend_content "${BUCKET_NAME}" "${REGION}" "${ACCOUNT_ID}")

  put_ssm_backend "$backend_content"
  log "Stored backend configuration into SSM parameter $SSM_PARAM_NAME"

  # Write backend file to target dir so the top-level stack can use the remote backend
  write_backend_hcl_to_file "$backend_content"

  # NOTE: We intentionally do NOT migrate the bootstrap local state into the bootstrap S3 bucket.
  # The bootstrap state remains local to the bootstrap directory to keep the bootstrap lifecycle separate.

  popd >/dev/null

}

ensure_backend_via_ssm_or_bootstrap() {
  log "ensure_backend_via_ssm_or_bootstrap: BEGIN"
  # Return with backend.hcl in $TARGET_DIR/backend.hcl ready and SSM param present
  local ssm_value
  ssm_value=$(get_ssm_backend) || ssm_value=""
  if [[ -n "$ssm_value" && "$ssm_value" != "None" ]]; then
    log "Found backend configuration in SSM $SSM_PARAM_NAME"
    write_backend_hcl_to_file "$ssm_value"
  else
    log "Backend SSM parameter $SSM_PARAM_NAME not found or empty. Running bootstrap to create backend and SSM entry."
    run_bootstrap_and_create_ssm
  fi
}

# Delete top-level stack (target dir)
delete_top_level_stack() {
  log "delete_top_level_stack: BEGIN"
  log "Destroying top-level stack in $TARGET_DIR"
  ensure_backend_via_ssm_or_bootstrap
  run_terraform_init_with_backend_file
  pushd "$TARGET_DIR" >/dev/null
  run_and_log terraform destroy -auto-approve -var "environment=${ENV}" -var "region=${REGION}"
  popd >/dev/null
  log "Top-level stack destroyed."
}

# Delete bootstrap resources S3 bucket
delete_bootstrap_stack() {
  log "delete_bootstrap_stack: BEGIN"
  # Find bootstrap dir
  BOOTSTRAP_LOCATIONS=("${TARGET_DIR}/bootstrap" "bootstrap")
  local found=""
  for dir in "${BOOTSTRAP_LOCATIONS[@]}"; do
    if [[ -d "$dir" ]]; then
      found="$dir"
      break
    fi
  done
  if [[ -z "$found" ]]; then
    log "Bootstrap directory not found; skipping bootstrap destroy."
    return 0
  fi

  log "Preparing to destroy bootstrap resources in $found"

  # Attempt to read backend info from SSM to get bucket name
  local ssm_value
  ssm_value=$(get_ssm_backend) || ssm_value=""

  # Try to ensure BUCKET_NAME is set even when SSM contained full backend HCL
  BUCKET_NAME=$(echo "$ssm_value" | sed -n 's/.*bucket *= *"\(.*\)".*/\1/p')
  BUCKET_NAME="${BUCKET_NAME:-${ACCOUNT_ID}-${SAFE_APP_NAME}-tfstate}"

  # Remove SSM param so subsequent runs won't pick up deleted backend
  delete_ssm_backend

  # Remove any temporary backend.tf left in the bootstrap dir
  erase_backend_tf "$found"

  # If a bucket name was determined, attempt to empty and delete it now (only used for destroy-all).
  if [[ -n "$BUCKET_NAME" ]]; then
    log "Emptying bootstrap S3 bucket $BUCKET_NAME in region $REGION"
    empty_s3_bucket "$BUCKET_NAME" "$REGION" || err "Failed to empty S3 bucket $BUCKET_NAME"
    log "Deleting bootstrap S3 bucket $BUCKET_NAME"
    if aws s3api delete-bucket --bucket "$BUCKET_NAME" --region "$REGION" >/dev/null 2>&1; then
      log "Deleted S3 bucket $BUCKET_NAME"
    else
      err "Failed to delete S3 bucket $BUCKET_NAME. Please delete it manually if it still exists."
    fi
  fi

  log "Bootstrap resources destroyed. If bucket deletion failed, empty and delete the S3 bucket manually."
}

# Main
synthesize_app_name_and_account

case "$COMMAND" in
  bootstrap)
    run_bootstrap_and_create_ssm
    log "Bootstrap completed. You can now run terraform init/plan/apply in $TARGET_DIR using the SSM-provided backend."
    ;;
  init)
    ensure_backend_via_ssm_or_bootstrap
    run_terraform_init_with_backend_file
    ;;
  plan)
    ensure_backend_via_ssm_or_bootstrap
    run_terraform_init_with_backend_file
    pushd "$TARGET_DIR" >/dev/null
    run_and_log terraform plan -input=false -var "environment=${ENV}" -var "region=${REGION}"
    popd >/dev/null
    ;;
  apply)
    ensure_backend_via_ssm_or_bootstrap
    run_terraform_init_with_backend_file
    pushd "$TARGET_DIR" >/dev/null
    run_and_log terraform apply -auto-approve -input=false -var "environment=${ENV}" -var "region=${REGION}"
    popd >/dev/null
    ;;
  destroy)
    if confirm_prompt "Destroy the top-level stack in $TARGET_DIR? This will permanently delete resources."; then
      delete_top_level_stack
    else
      log "Aborted top-level destroy."
    fi
    ;;
  destroy-all)
    if confirm_prompt "Destroy the top-level stack and bootstrap S3 bucket? This will permanently delete resources and remove the backend SSM entry."; then
      delete_top_level_stack
      delete_bootstrap_stack
    else
      log "Aborted destroy-all."
    fi
    ;;
  *)
    # default: init only (backwards compatible)
    print_usage
    exit 1
    ;;
esac

log "Done." 