#!/usr/bin/env bash
#
# Create, update or destroy the forge-tool AWS resources with Terraform.
#
#   ./infra/deploy.sh                    # apply all three parts
#   ./infra/deploy.sh --sync             # apply, upload docs, start ingestion
#   ./infra/deploy.sh --sync-only        # upload docs + start ingestion only
#   ./infra/deploy.sh --plan             # show the plan, change nothing
#   ./infra/deploy.sh --no-kb            # leave the knowledge base untouched this run
#   ./infra/deploy.sh --no-guardrail     # leave the guardrail untouched this run
#   ./infra/deploy.sh --no-permissions   # leave the IAM policy untouched this run
#   ./infra/deploy.sh --kb-only          # only the knowledge base
#   ./infra/deploy.sh --guardrail-only   # only the guardrail
#   ./infra/deploy.sh --permissions-only # only the IAM policy
#   ./infra/deploy.sh --delete           # terraform destroy: everything, buckets included
#   ./infra/deploy.sh --yes              # skip the destroy confirmation
#
# Parts: knowledge base (S3 Vectors), Bedrock guardrail, IAM runtime policy -
# see infra/terraform/. Every value comes from the repo-root .env and reaches
# Terraform as TF_VAR_<lowercase name>. On success the knowledge base id and
# the guardrail id/version are written back to .env.
#
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${INFRA_DIR}/.." && pwd)"
TF_DIR="${INFRA_DIR}/terraform"
ENV_FILE="${REPO_ROOT}/.env"

# shellcheck source=lib/env.sh
source "${INFRA_DIR}/lib/env.sh"

ACTION=apply          # apply | plan | destroy
SYNC_DOCS=false
SKIP_APPLY=false
ASSUME_YES=false
DO_KB=true
DO_GUARDRAIL=true
DO_PERMISSIONS=true

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
}

for arg in "$@"; do
  case "$arg" in
    --sync)             SYNC_DOCS=true ;;
    --sync-only)        SYNC_DOCS=true; SKIP_APPLY=true ;;
    --plan)             ACTION=plan ;;
    --no-kb)            DO_KB=false ;;
    --no-guardrail)     DO_GUARDRAIL=false ;;
    --no-permissions)   DO_PERMISSIONS=false ;;
    --kb-only)          DO_GUARDRAIL=false; DO_PERMISSIONS=false ;;
    --guardrail-only)   DO_KB=false; DO_PERMISSIONS=false ;;
    --permissions-only) DO_KB=false; DO_GUARDRAIL=false ;;
    --delete)           ACTION=destroy ;;
    --yes)              ASSUME_YES=true ;;
    -h|--help)          usage ;;
    *)                  die "Unknown option: $arg (try --help)" ;;
  esac
done

# --- configuration ---------------------------------------------------------
[[ -f "$ENV_FILE" ]] || die ".env not found. Run: cp .env.example .env"
forge_load_env "$ENV_FILE"

AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT_NAME="${PROJECT_NAME:-forge-tool}"
PARAMETER_STORE_PREFIX="${PARAMETER_STORE_PREFIX:-forge_tool_}"
KB_DOCS_PREFIX="${KB_DOCS_PREFIX:-guidelines/}"
KNOWLEDGE_BASE_DIRECTORY="${KNOWLEDGE_BASE_DIRECTORY:-./knowledge-base}"
export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION"

# The ids this script writes to .env (KB id, guardrail id) are region-specific.
# If the effective region came from an exported variable and differs from the
# region in the file, writing them would hand the app ids from the wrong region.
ENV_FILE_REGION="$(sed -n 's/^AWS_REGION=//p' "$ENV_FILE" | head -1 | tr -d "\"'" | tr -d '[:space:]')"
WRITE_IDS=true
if [[ -n "$ENV_FILE_REGION" && "$ENV_FILE_REGION" != "$AWS_REGION" ]]; then
  warn "Deploying to ${AWS_REGION}, but .env has AWS_REGION=${ENV_FILE_REGION}."
  warn "Resource ids will NOT be written to .env; the app resolves them from SSM in"
  warn "its own region. Set AWS_REGION=${AWS_REGION} in .env to make it the default."
  WRITE_IDS=false
fi

command -v aws >/dev/null       || die "aws CLI not found. Install the AWS CLI v2."
command -v terraform >/dev/null || die "terraform not found. Install Terraform >= 1.6 (brew install hashicorp/tap/terraform)."
aws sts get-caller-identity >/dev/null 2>&1 \
  || die "No valid AWS credentials. Export credentials and retry."

# Hand every infra setting to Terraform as TF_VAR_<lowercase>. Empty values are
# skipped so the variable defaults in infra/terraform/variables.tf apply.
TF_VAR_NAMES="AWS_REGION PROJECT_NAME PARAMETER_STORE_PREFIX
  CREATE_KNOWLEDGE_BASE CREATE_GUARDRAIL CREATE_PERMISSIONS
  KB_NAME KB_DESCRIPTION KB_DATA_SOURCE_NAME KB_EMBEDDING_MODEL_ID KB_EMBEDDING_DIMENSIONS
  KB_DISTANCE_METRIC KB_CHUNK_MAX_TOKENS KB_CHUNK_OVERLAP_PERCENT KB_VECTOR_BUCKET_NAME
  KB_VECTOR_INDEX_NAME KB_DOCS_BUCKET_NAME KB_DOCS_PREFIX
  GUARDRAIL_NAME GUARDRAIL_PII_ACTION
  APP_IAM_PRINCIPAL BEDROCK_MODEL_ID PRIMARY_BASE_MODEL_ID REVIEWER_MODEL_ID REVIEWER_BASE_MODEL_ID"
for name in $TF_VAR_NAMES; do
  value="${!name:-}"
  [[ -n "$value" ]] || continue
  lower="$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]')"
  export "TF_VAR_${lower}=${value}"
done

# --- helpers ---------------------------------------------------------------
tf() { terraform -chdir="$TF_DIR" "$@"; }

tf_output() {  # name -> value or ""
  tf output -raw "$1" 2>/dev/null || true
}

write_env() {  # key value - update or append KEY=value in .env
  local key="$1" value="$2" tmp
  if [[ "$WRITE_IDS" != true ]]; then
    log "Not writing ${key}=${value} to .env (region differs from .env)"
    return 0
  fi
  if grep -q "^${key}=" "$ENV_FILE"; then
    tmp="$(mktemp)"
    sed "s|^${key}=.*|${key}=${value}|" "$ENV_FILE" > "$tmp"
    mv "$tmp" "$ENV_FILE"
  else
    printf '\n%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}

# -target flags for a partial run; empty when all three parts are selected.
TARGETS=()
if [[ "$DO_KB" != true || "$DO_GUARDRAIL" != true || "$DO_PERMISSIONS" != true ]]; then
  [[ "$DO_KB" == true ]]          && TARGETS+=(-target=module.knowledge_base)
  [[ "$DO_GUARDRAIL" == true ]]   && TARGETS+=(-target=module.guardrail)
  [[ "$DO_PERMISSIONS" == true ]] && TARGETS+=(-target=module.permissions)
  [[ ${#TARGETS[@]} -gt 0 ]] || die "Every part is excluded; nothing to do."
  # The IAM policy is scoped to the guardrail and knowledge base ids, so Terraform
  # treats those modules as dependencies of module.permissions and includes them
  # whenever permissions is targeted. Say so, rather than let --no-kb silently
  # create a knowledge base.
  if [[ "$DO_PERMISSIONS" == true && ( "$DO_KB" != true || "$DO_GUARDRAIL" != true ) ]]; then
    warn "module.permissions depends on the guardrail and knowledge base, so an excluded"
    warn "part is still created if missing (or updated if its config changed). To run"
    warn "without one, set CREATE_KNOWLEDGE_BASE/CREATE_GUARDRAIL=false in .env instead."
    warn "Use --plan to see exactly what this run touches."
  fi
fi

log "Terraform init (${TF_DIR})"
tf init -input=false -no-color >/dev/null

# --- plan ------------------------------------------------------------------
if [[ "$ACTION" == plan ]]; then
  tf plan -input=false -no-color ${TARGETS[@]+"${TARGETS[@]}"}
  exit 0
fi

# --- destroy ---------------------------------------------------------------
if [[ "$ACTION" == destroy ]]; then
  warn "This destroys the selected parts in ${AWS_REGION}, INCLUDING the documents"
  warn "bucket, vector bucket and index (docs are a copy of ${KNOWLEDGE_BASE_DIRECTORY};"
  warn "vectors are regenerated on re-ingest). The PAT in SSM is not touched."
  if [[ "$ASSUME_YES" != true ]]; then
    read -r -p "Type the project name to confirm (${PROJECT_NAME}): " confirm
    [[ "$confirm" == "$PROJECT_NAME" ]] || die "Aborted."
  fi
  tf destroy -input=false -auto-approve -no-color ${TARGETS[@]+"${TARGETS[@]}"}
  [[ "$DO_KB" == true ]]        && write_env KNOWLEDGE_BASE_ID ""
  [[ "$DO_GUARDRAIL" == true ]] && { write_env GUARDRAIL_ID ""; write_env GUARDRAIL_VERSION ""; write_env PII_SCAN_GUARDRAIL_ID ""; write_env PII_SCAN_GUARDRAIL_VERSION ""; }
  log "Destroyed."
  exit 0
fi

# --- apply -----------------------------------------------------------------
if [[ "$SKIP_APPLY" != true ]]; then
  log "Terraform apply${TARGETS[*]:+ (${TARGETS[*]})}"
  tf apply -input=false -auto-approve -no-color ${TARGETS[@]+"${TARGETS[@]}"}
fi

KB_ID="$(tf_output knowledge_base_id)"
DATA_SOURCE_ID="$(tf_output data_source_id)"
DOCS_BUCKET="$(tf_output docs_bucket_name)"
VECTOR_INDEX_ARN="$(tf_output vector_index_arn)"
GUARDRAIL_ID="$(tf_output guardrail_id)"
GUARDRAIL_VERSION="$(tf_output guardrail_version)"
PII_SCAN_GUARDRAIL_ID="$(tf_output pii_scan_guardrail_id)"
PII_SCAN_GUARDRAIL_VERSION="$(tf_output pii_scan_guardrail_version)"
POLICY_ARN="$(tf_output app_policy_arn)"
ATTACHED_TO="$(tf_output app_policy_attached_to)"

# --- upload guideline docs and ingest --------------------------------------
if [[ "$SYNC_DOCS" == true ]]; then
  if [[ -z "$KB_ID" || -z "$DOCS_BUCKET" ]]; then
    warn "No knowledge base in the Terraform state; skipping --sync."
  else
    docs_dir="$KNOWLEDGE_BASE_DIRECTORY"
    [[ "$docs_dir" = /* ]] || docs_dir="${REPO_ROOT}/${docs_dir#./}"
    if [[ -d "$docs_dir" ]]; then
      log "Uploading ${docs_dir} to s3://${DOCS_BUCKET}/${KB_DOCS_PREFIX}"
      aws s3 sync "$docs_dir" "s3://${DOCS_BUCKET}/${KB_DOCS_PREFIX}" --delete
    else
      warn "${docs_dir} does not exist; skipping upload."
    fi
    log "Starting ingestion job"
    job_id="$(aws bedrock-agent start-ingestion-job \
      --knowledge-base-id "$KB_ID" --data-source-id "$DATA_SOURCE_ID" \
      --query 'ingestionJob.ingestionJobId' --output text)"
    log "Ingestion job ${job_id} started. Track it with:"
    printf '      aws bedrock-agent get-ingestion-job --knowledge-base-id %s \\\n' "$KB_ID"
    printf '        --data-source-id %s --ingestion-job-id %s\n' "$DATA_SOURCE_ID" "$job_id"
  fi
fi

# --- write ids back to .env -----------------------------------------------
[[ -n "$KB_ID" ]]        && write_env KNOWLEDGE_BASE_ID "$KB_ID"
[[ -n "$GUARDRAIL_ID" ]] && { write_env GUARDRAIL_ID "$GUARDRAIL_ID"; write_env GUARDRAIL_VERSION "$GUARDRAIL_VERSION"; }
[[ -n "$PII_SCAN_GUARDRAIL_ID" ]] && { write_env PII_SCAN_GUARDRAIL_ID "$PII_SCAN_GUARDRAIL_ID"; write_env PII_SCAN_GUARDRAIL_VERSION "$PII_SCAN_GUARDRAIL_VERSION"; }

# --- summary ---------------------------------------------------------------
echo
log "Done (${AWS_REGION})"
[[ -n "$KB_ID" ]] && cat <<EOF
  Knowledge base id   : ${KB_ID}
  Data source id      : ${DATA_SOURCE_ID}
  Documents bucket    : s3://${DOCS_BUCKET}/${KB_DOCS_PREFIX}
  Vector index        : ${VECTOR_INDEX_ARN}
EOF
[[ -n "$GUARDRAIL_ID" ]] && cat <<EOF
  Guardrail           : ${GUARDRAIL_ID} version ${GUARDRAIL_VERSION}
  PII scan guardrail  : ${PII_SCAN_GUARDRAIL_ID:-none} version ${PII_SCAN_GUARDRAIL_VERSION:-none}
EOF
[[ -n "$POLICY_ARN" ]] && cat <<EOF
  Runtime policy      : ${POLICY_ARN}  (attached to: ${ATTACHED_TO:-nothing})
EOF
cat <<EOF

Ids are in .env and in SSM under the ${PARAMETER_STORE_PREFIX} prefix. Add guideline
documents under ${KNOWLEDGE_BASE_DIRECTORY} and re-run with --sync-only to re-ingest.
EOF
