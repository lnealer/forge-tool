#!/usr/bin/env bash
#
# Store the forge-tool secrets in AWS SSM Parameter Store as SecureString
# parameters. They are deliberately NOT managed by Terraform: a SecureString
# resource would copy the secret value into the state file, so the PAT is
# written with the CLI and only ever referenced by name.
#
# Parameters written (prefix comes from PARAMETER_STORE_PREFIX in .env):
#   ${PREFIX}api_key          - GitHub PAT: clone, push and open PRs over HTTPS
#   ${PREFIX}ssh_private_key  - optional legacy SSH key (not used by the app today)
#
# Usage:
#   ./infra/put_ssm_parameters.sh                      # prompt for the PAT
#   ./infra/put_ssm_parameters.sh --pat-only           # same (kept for compatibility)
#   ./infra/put_ssm_parameters.sh --ssh-key ~/.ssh/id_ed25519   # also store an SSH key
#   ./infra/put_ssm_parameters.sh --ssh-key-only --ssh-key ~/.ssh/id_ed25519
#   ./infra/put_ssm_parameters.sh --show               # list without values
#
# The GitHub PAT needs `repo` scope (classic) or Contents + Pull requests
# read/write (fine-grained) on the repositories being upgraded. Values are
# passed to the CLI through a 0600 temp file so they never appear in argv
# or shell history.
#
set -euo pipefail

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${INFRA_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"

# shellcheck source=lib/env.sh
source "${INFRA_DIR}/lib/env.sh"

SSH_KEY_PATH=""
KMS_KEY_ID=""
DO_PAT=true
DO_SSH=false
SHOW_ONLY=false

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ssh-key)      SSH_KEY_PATH="${2:?--ssh-key needs a path}"; DO_SSH=true; shift 2 ;;
    --kms-key-id)   KMS_KEY_ID="${2:?--kms-key-id needs a value}"; shift 2 ;;
    --pat-only)     DO_SSH=false; shift ;;
    --ssh-key-only) DO_PAT=false; DO_SSH=true; shift ;;
    --show)         SHOW_ONLY=true; shift ;;
    -h|--help)      usage ;;
    *)              die "Unknown option: $1 (try --help)" ;;
  esac
done

if [[ -f "$ENV_FILE" ]]; then
  forge_load_env "$ENV_FILE"
else
  warn ".env not found; falling back to defaults."
fi

AWS_REGION="${AWS_REGION:-us-east-1}"
PARAMETER_STORE_PREFIX="${PARAMETER_STORE_PREFIX:-forge_tool_}"
export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION"

PAT_PARAM="${PARAMETER_STORE_PREFIX}api_key"
SSH_PARAM="${PARAMETER_STORE_PREFIX}ssh_private_key"

command -v aws >/dev/null || die "aws CLI not found. Install the AWS CLI v2."
command -v python3 >/dev/null || die "python3 not found."
aws sts get-caller-identity >/dev/null 2>&1 \
  || die "No valid AWS credentials. Export credentials and retry."

if [[ "$SHOW_ONLY" == true ]]; then
  log "Parameters in region ${AWS_REGION} with prefix '${PARAMETER_STORE_PREFIX}'"
  aws ssm describe-parameters \
    --parameter-filters "Key=Name,Option=BeginsWith,Values=${PARAMETER_STORE_PREFIX}" \
    --query 'Parameters[].{Name:Name,Type:Type,Updated:LastModifiedDate}' \
    --output table
  exit 0
fi

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT INT TERM
chmod 700 "$WORK_DIR"

# Build the request body with python so the value is JSON-escaped correctly
# (private keys contain newlines) and never lands in the process table.
put_secure_parameter() {  # name description value-file [kind]
  local name="$1" description="$2" value_file="$3" kind="${4:-}"
  local payload="${WORK_DIR}/payload.json"

  ( umask 077; : > "$payload" )
  PARAM_NAME="$name" PARAM_DESC="$description" PARAM_KIND="$kind" \
  PARAM_VALUE_FILE="$value_file" PARAM_KMS="$KMS_KEY_ID" \
  python3 - "$payload" <<'PY'
import json, os, sys

with open(os.environ["PARAM_VALUE_FILE"]) as handle:
    value = handle.read()

if not value.strip():
    sys.exit("Refusing to store an empty value.")

if os.environ.get("PARAM_KIND") == "github_pat":
    # Pasted tokens routinely pick up a zero-width space, a BOM or a stray
    # newline; one invisible character makes GitHub answer "Bad credentials"
    # and makes git reject the clone URL as malformed. Strip anything that is
    # not printable ASCII, then insist on a real token shape.
    import re, unicodedata
    stray = [(i, f"U+{ord(c):04X} {unicodedata.name(c, '?')}") for i, c in enumerate(value)
             if not (c.isascii() and c.isprintable()) or c.isspace()]
    cleaned = "".join(c for c in value if c.isascii() and c.isprintable() and not c.isspace())
    if stray and stray != [(len(value) - 1, "U+000A LINE FEED")]:
        print("note: removed non-printable characters from the pasted token:", file=sys.stderr)
        for i, name in stray:
            print(f"  index {i}: {name}", file=sys.stderr)
    shapes = (r"ghp_[A-Za-z0-9]{36}", r"gh[osur]_[A-Za-z0-9]{36}", r"github_pat_[A-Za-z0-9_]{82}")
    if not any(re.fullmatch(p, cleaned) for p in shapes):
        sys.exit(f"That does not look like a GitHub token ({len(cleaned)} chars after cleaning; "
                 "expected ghp_ + 36, gho_/ghs_/ghu_/ghr_ + 36, or github_pat_ + 82). Not stored.")
    value = cleaned

body = {
    "Name": os.environ["PARAM_NAME"],
    "Description": os.environ["PARAM_DESC"],
    "Value": value,
    "Type": "SecureString",
    "Overwrite": True,
    "Tier": "Standard",
}
if os.environ.get("PARAM_KMS"):
    body["KeyId"] = os.environ["PARAM_KMS"]

with open(sys.argv[1], "w") as handle:
    json.dump(body, handle)
PY

  aws ssm put-parameter --cli-input-json "file://${payload}" \
    --query 'Version' --output text >/dev/null
  rm -f "$payload"
  log "Wrote ${name}"
}

# --- GitHub PAT ------------------------------------------------------------
if [[ "$DO_PAT" == true ]]; then
  pat_file="${WORK_DIR}/pat"
  ( umask 077; : > "$pat_file" )
  if [[ -n "${GITHUB_PAT:-}" ]]; then
    log "Using GITHUB_PAT from the environment"
    printf '%s' "$GITHUB_PAT" > "$pat_file"
  else
    printf 'GitHub personal access token (input hidden): '
    read -rs pat_value
    printf '\n'
    [[ -n "$pat_value" ]] || die "No token entered."
    printf '%s' "$pat_value" > "$pat_file"
    unset pat_value
  fi
  put_secure_parameter "$PAT_PARAM" \
    "forge-tool GitHub personal access token (used to open pull requests)" \
    "$pat_file" github_pat
  rm -f "$pat_file"
fi

# --- git SSH private key ---------------------------------------------------
if [[ "$DO_SSH" == true ]]; then
  if [[ -z "$SSH_KEY_PATH" ]]; then
    read -r -p "Path to the git SSH private key [${HOME}/.ssh/id_ed25519]: " SSH_KEY_PATH
    SSH_KEY_PATH="${SSH_KEY_PATH:-${HOME}/.ssh/id_ed25519}"
  fi
  SSH_KEY_PATH="${SSH_KEY_PATH/#\~/$HOME}"
  [[ -f "$SSH_KEY_PATH" ]] || die "SSH key not found: ${SSH_KEY_PATH}"
  if [[ "$SSH_KEY_PATH" == *.pub ]]; then
    die "${SSH_KEY_PATH} looks like a public key. Pass the private key."
  fi
  # Works for both PEM and the newer OPENSSH key format, unlike grepping for
  # "ENCRYPTED" which only appears in old PEM headers.
  if command -v ssh-keygen >/dev/null 2>&1; then
    if ! ssh-keygen -y -P "" -f "$SSH_KEY_PATH" >/dev/null 2>&1; then
      die "${SSH_KEY_PATH} is not a usable unencrypted private key. The agent clones non-interactively, so use a passphrase-free deploy key."
    fi
  fi
  put_secure_parameter "$SSH_PARAM" \
    "forge-tool git SSH private key (used to clone and push)" \
    "$SSH_KEY_PATH"
fi

log "Done. Verify with: ./infra/put_ssm_parameters.sh --show"
