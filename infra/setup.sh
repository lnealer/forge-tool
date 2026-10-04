#!/usr/bin/env bash
#
# Local environment setup and launcher for forge-tool.
#
#   ./infra/setup.sh                   # install deps, then run the server in the foreground
#   ./infra/setup.sh install           # install deps only
#   ./infra/setup.sh run               # Streamlit in the foreground (Ctrl-C stops it)
#   ./infra/setup.sh start             # Streamlit detached, logging to app.log
#   ./infra/setup.sh status            # pid, uptime, health, connected browsers, run activity
#   ./infra/setup.sh stop [--force]    # stop it; refuses while a browser is connected or a run is active
#   ./infra/setup.sh restart [--force] # stop + start, same guard
#   ./infra/setup.sh check             # verify the toolchain and AWS config
#   ./infra/setup.sh config            # print the effective configuration and where each value came from
#   source infra/setup.sh              # only export PATH/PYTHONPATH/JAVA_HOME
#
# Pass extra arguments through to the app after run/start:
#   ./infra/setup.sh run --github_url https://github.com/org/repo.git --upgrade_details "Java 21"
#
# All configuration comes from the repo-root .env (see .env.example). Every
# install/run/start ends by printing the effective configuration: what was
# set, and whether it came from the shell, .env or a default. Secrets are
# shown as set/unset, never printed.
#
set -uo pipefail

# --- locate the repo whether executed or sourced ---------------------------
if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
  _FORGE_SRC="${BASH_SOURCE[0]}"
else
  _FORGE_SRC="$0"
fi
FORGE_SOURCED=false
[[ "${_FORGE_SRC}" != "$0" ]] && FORGE_SOURCED=true

INFRA_DIR="$(cd "$(dirname "${_FORGE_SRC}")" && pwd)"
REPO_ROOT="$(cd "${INFRA_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"
PYTHON_DIR="${REPO_ROOT}/python"

# shellcheck source=lib/env.sh
source "${INFRA_DIR}/lib/env.sh"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; }

# When sourced, never kill the user's shell on error.
fail() {
  err "$*"
  if [[ "$FORGE_SOURCED" == true ]]; then return 1; else exit 1; fi
}

# --- .env ------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -f "${REPO_ROOT}/.env.example" ]]; then
    log "Creating .env from .env.example"
    cp "${REPO_ROOT}/.env.example" "$ENV_FILE"
  else
    fail ".env and .env.example are both missing."
  fi
fi

# Names exported before .env is read: those values came from the shell and
# win over the file, which the configuration summary points out.
_FORGE_SHELL_KEYS=" $(compgen -e | tr '\n' ' ') "
forge_load_env "$ENV_FILE"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-.venv}"
[[ "$VENV_DIR" = /* ]] || VENV_DIR="${REPO_ROOT}/${VENV_DIR#./}"
VENV_BIN="${VENV_DIR}/bin"

AWS_REGION="${AWS_REGION:-us-east-1}"
APP_ENTRYPOINT="${APP_ENTRYPOINT:-python/chat.py}"
[[ "$APP_ENTRYPOINT" = /* ]] || APP_ENTRYPOINT="${REPO_ROOT}/${APP_ENTRYPOINT#./}"
STREAMLIT_SERVER_PORT="${STREAMLIT_SERVER_PORT:-8501}"
STREAMLIT_SERVER_ADDRESS="${STREAMLIT_SERVER_ADDRESS:-localhost}"
STREAMLIT_SERVER_HEADLESS="${STREAMLIT_SERVER_HEADLESS:-false}"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
set_paths() {
  # JAVA_HOME resolution. JAVA_VERSION is an explicit pin, so it wins over an
  # inherited JAVA_HOME; without it, JAVA_HOME (from the shell or .env) is used,
  # then java_home's default, then the javac on PATH.
  JAVA_HOME_SOURCE="unset"
  local resolved=""
  if [[ -n "${JAVA_VERSION:-}" && -x /usr/libexec/java_home ]]; then
    resolved="$(/usr/libexec/java_home -v "$JAVA_VERSION" 2>/dev/null)" || resolved=""
    [[ -z "$resolved" ]] && warn "No JDK ${JAVA_VERSION} found via java_home; falling back."
  fi
  if [[ -n "$resolved" ]]; then
    JAVA_HOME="$resolved"
    JAVA_HOME_SOURCE="JAVA_VERSION=${JAVA_VERSION}"
    # java_home treats -v as "this version or later", so a pin of 21 silently
    # resolves to 17 when 21 is not installed. Say so rather than running the
    # Maven build on the wrong JDK without a word.
    local want_major="${JAVA_VERSION%%.*}" got_major=""
    got_major="$(sed -n 's/^JAVA_VERSION="\([0-9][0-9]*\).*/\1/p' "${resolved}/release" 2>/dev/null | head -1)"
    if [[ -n "$got_major" && -n "$want_major" && "$got_major" != "$want_major" ]]; then
      warn "JAVA_VERSION=${JAVA_VERSION} resolved to Java ${got_major} (JDK ${want_major} is not installed)."
      JAVA_HOME_SOURCE="JAVA_VERSION=${JAVA_VERSION}, got Java ${got_major}"
    fi
  elif [[ -n "${JAVA_HOME:-}" ]]; then
    JAVA_HOME_SOURCE="JAVA_HOME"
  elif [[ -x /usr/libexec/java_home ]] && resolved="$(/usr/libexec/java_home 2>/dev/null)"; then
    JAVA_HOME="$resolved"
    JAVA_HOME_SOURCE="java_home default"
  elif command -v javac >/dev/null 2>&1; then
    local javac_path
    javac_path="$(command -v javac)"
    JAVA_HOME="$(dirname "$(dirname "$(readlink -f "$javac_path" 2>/dev/null || printf '%s' "$javac_path")")")"
    JAVA_HOME_SOURCE="javac on PATH"
  fi
  if [[ -n "${JAVA_HOME:-}" ]]; then
    export JAVA_HOME
    export PATH="${JAVA_HOME}/bin:${PATH}"
  fi

  # Maven honours MAVEN_OPTS; keep the agent's `mvn` runs off the interactive
  # transfer progress output so tool results stay readable.
  export MAVEN_OPTS="${MAVEN_OPTS:--Xmx2g}"
  export MAVEN_ARGS="${MAVEN_ARGS:--B -ntp}"

  # The app imports its modules as top-level names (settings, utils, agent...).
  case ":${PYTHONPATH:-}:" in
    *":${PYTHON_DIR}:"*) ;;
    *) export PYTHONPATH="${PYTHON_DIR}${PYTHONPATH:+:${PYTHONPATH}}" ;;
  esac

  [[ -d "$VENV_BIN" ]] && export PATH="${VENV_BIN}:${PATH}"

  export AWS_REGION
  export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-$AWS_REGION}"
  export FORGE_TOOL_ROOT="$REPO_ROOT"
}

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
do_install() {
  command -v "$PYTHON_BIN" >/dev/null || { fail "${PYTHON_BIN} not found."; return 1; }

  if [[ ! -d "$VENV_DIR" ]]; then
    log "Creating virtualenv at ${VENV_DIR}"
    "$PYTHON_BIN" -m venv "$VENV_DIR" || { fail "Could not create the virtualenv."; return 1; }
  else
    log "Reusing virtualenv at ${VENV_DIR}"
  fi

  set_paths

  log "Upgrading pip"
  "${VENV_BIN}/python" -m pip install --upgrade pip --quiet || return 1

  log "Installing ${PYTHON_DIR}/requirements.txt"
  "${VENV_BIN}/python" -m pip install -r "${PYTHON_DIR}/requirements.txt" || return 1

  log "Install complete."
  do_check || warn "Continuing anyway; fix the items above before running an upgrade."
  show_config
}

# ---------------------------------------------------------------------------
# Check
# ---------------------------------------------------------------------------
do_check() {
  set_paths
  local problems=0

  log "Toolchain"
  for tool in "$PYTHON_BIN" mvn java git aws terraform; do
    if command -v "$tool" >/dev/null 2>&1; then
      printf '  ok     %-8s %s\n' "$tool" "$(command -v "$tool")"
    else
      printf '  MISSING %-7s (%s)\n' "$tool" "$([[ $tool == terraform ]] && echo 'needed for ./infra/deploy.sh' || echo 'needed to run code upgrades')"
      problems=$((problems + 1))
    fi
  done
  printf '  JAVA_HOME = %s (from %s)\n' "${JAVA_HOME:-<unset>}" "${JAVA_HOME_SOURCE:-unset}"
  printf '  PYTHONPATH = %s\n' "${PYTHONPATH:-<unset>}"

  log "AWS"
  if aws sts get-caller-identity >/dev/null 2>&1; then
    printf '  ok     credentials  %s\n' "$(aws sts get-caller-identity --query Arn --output text)"
    printf '  region %s\n' "$AWS_REGION"
  else
    printf '  MISSING valid AWS credentials (export them before running)\n'
    problems=$((problems + 1))
  fi

  log "Knowledge base"
  if [[ -n "${KNOWLEDGE_BASE_ID:-}" ]]; then
    printf '  ok     KNOWLEDGE_BASE_ID=%s (from .env)\n' "$KNOWLEDGE_BASE_ID"
  else
    local param="${PARAMETER_STORE_PREFIX:-forge_tool_}knowledge_base_id"
    if aws ssm get-parameter --name "$param" >/dev/null 2>&1; then
      printf '  ok     resolved from SSM parameter %s\n' "$param"
    else
      printf '  none   no knowledge base yet (optional - the agent runs without it)\n'
      printf '         create one with: ./infra/deploy.sh --sync\n'
    fi
  fi

  log "Guardrail"
  if [[ -n "${GUARDRAIL_ID:-}" ]]; then
    printf '  ok     GUARDRAIL_ID=%s (from .env)\n' "$GUARDRAIL_ID"
  elif aws ssm get-parameter --name "${PARAMETER_STORE_PREFIX:-forge_tool_}guardrail_id" >/dev/null 2>&1; then
    printf '  ok     resolved from SSM parameter %sguardrail_id\n' "${PARAMETER_STORE_PREFIX:-forge_tool_}"
  else
    printf '  none   no Bedrock guardrail yet (optional - local secret scanning still runs)\n'
    printf '         create one with: ./infra/deploy.sh --guardrail-only\n'
  fi
  printf '  reviewer %s (%s)\n' "${REVIEWER_ENABLED:-true}" "${REVIEWER_MODEL_ID:-us.amazon.nova-pro-v1:0}"

  log "Secrets"
  local prefix="${PARAMETER_STORE_PREFIX:-forge_tool_}"
  local names="${SSM_PARAMETER_NAMES:-ssh_private_key,api_key}"
  for name in ${names//,/ }; do
    if aws ssm get-parameter --name "${prefix}${name}" >/dev/null 2>&1; then
      printf '  ok     %s\n' "${prefix}${name}"
    else
      printf '  MISSING %s. Run: ./infra/put_ssm_parameters.sh\n' "${prefix}${name}"
      problems=$((problems + 1))
    fi
  done

  if [[ "$problems" -gt 0 ]]; then
    warn "${problems} item(s) need attention."
    return 1
  fi
  log "Everything checks out."
}

# ---------------------------------------------------------------------------
# Run the Streamlit server
# ---------------------------------------------------------------------------
build_streamlit_cmd() {
  # Resolve the binary and assemble the argv used by both run (foreground)
  # and start (detached). Sets STREAMLIT_BIN and STREAMLIT_ARGS.
  set_paths
  STREAMLIT_BIN="${VENV_BIN}/streamlit"
  [[ -x "$STREAMLIT_BIN" ]] || STREAMLIT_BIN="$(command -v streamlit 2>/dev/null || true)"
  if [[ -z "$STREAMLIT_BIN" ]]; then
    fail "streamlit not installed. Run: ./infra/setup.sh install"
    return 1
  fi

  local app_args=("$@")
  # Without args, fall back to the .env defaults so the server starts without
  # waiting on an interactive prompt the browser cannot answer.
  if [[ ${#app_args[@]} -eq 0 ]]; then
    [[ -n "${DEFAULT_GITHUB_URL:-}" ]] && app_args+=(--github_url "$DEFAULT_GITHUB_URL")
    [[ -n "${DEFAULT_UPGRADE_DETAILS:-}" ]] && app_args+=(--upgrade_details "$DEFAULT_UPGRADE_DETAILS")
  fi
  [[ ${#app_args[@]} -gt 0 ]] && log "App arguments: ${app_args[*]}"

  STREAMLIT_ARGS=(
    run "$APP_ENTRYPOINT"
    --server.port "$STREAMLIT_SERVER_PORT"
    --server.address "$STREAMLIT_SERVER_ADDRESS"
    --server.headless "$STREAMLIT_SERVER_HEADLESS"
  )
  # Everything after `--` is forwarded to the app's argparse.
  if [[ ${#app_args[@]} -gt 0 ]]; then
    STREAMLIT_ARGS+=(-- "${app_args[@]}")
  fi
}

do_run() {
  build_streamlit_cmd "$@" || return 1
  # Printed before Streamlit takes over the terminal.
  show_config
  # Also write to app.log (rotated, like start) so status/stop see a foreground
  # run's activity and the run can be inspected afterwards.
  [[ -f "$APP_LOG" ]] && mv -f "$APP_LOG" "${APP_LOG}.1"
  log "Starting Streamlit on http://${STREAMLIT_SERVER_ADDRESS}:${STREAMLIT_SERVER_PORT} (also logging to ${APP_LOG})"
  PYTHONUNBUFFERED=1 "$STREAMLIT_BIN" "${STREAMLIT_ARGS[@]}" 2>&1 | tee -a "$APP_LOG"
}

# ---------------------------------------------------------------------------
# Server lifecycle (detached mode)
# ---------------------------------------------------------------------------
APP_LOG="${REPO_ROOT}/app.log"
RUN_ACTIVE_MINUTES="${RUN_ACTIVE_MINUTES:-5}"

server_url()    { printf 'http://%s:%s' "$STREAMLIT_SERVER_ADDRESS" "$STREAMLIT_SERVER_PORT"; }
server_health() { curl -fsS -m 2 "$(server_url)/_stcore/health" 2>/dev/null; }
server_pid()    { lsof -nP -iTCP:"$STREAMLIT_SERVER_PORT" -sTCP:LISTEN -t 2>/dev/null | head -1; }

browser_sessions() {  # client-side sockets to the port, i.e. connected browser tabs/websockets
  local pid; pid="$(server_pid)"
  lsof -nP -iTCP:"$STREAMLIT_SERVER_PORT" -sTCP:ESTABLISHED 2>/dev/null \
    | awk -v p="$pid" 'NR > 1 && $2 != p' | wc -l | tr -d ' '
}

# The app only writes app.log while a turn is progressing (agent init, tool
# calls, git, maven), so a recent modification time means a run is under way.
# Sets LAST_ACTIVITY_AGE (seconds) and LAST_ACTIVITY_LINE.
run_looks_active() {
  LAST_ACTIVITY_AGE=""; LAST_ACTIVITY_LINE=""
  [[ -f "$APP_LOG" ]] || return 1
  local mtime
  mtime="$(stat -f %m "$APP_LOG" 2>/dev/null || stat -c %Y "$APP_LOG" 2>/dev/null)" || return 1
  LAST_ACTIVITY_AGE=$(( $(date +%s) - mtime ))
  LAST_ACTIVITY_LINE="$(grep -E '→ |← |Running: |Cloning repo|Secret guardrail|Reviewer' "$APP_LOG" \
    | tail -1 | sed 's/\x1b\[[0-9;]*m//g' | sed 's/^INFO:FORGE_TOOL://' | cut -c1-110)"
  [[ "$LAST_ACTIVITY_AGE" -lt $(( RUN_ACTIVE_MINUTES * 60 )) ]]
}

do_status() {
  local pid; pid="$(server_pid)"
  if [[ -z "$pid" ]]; then
    log "Streamlit: not running (nothing listens on port ${STREAMLIT_SERVER_PORT})"
    return 1
  fi
  local up health
  up="$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ')"
  health="$(server_health || echo DOWN)"
  log "Streamlit: pid ${pid}, up ${up}, health ${health}, $(server_url)"
  printf '  browser sessions : %s\n' "$(browser_sessions)"
  if run_looks_active; then
    printf '  activity         : app.log changed %ss ago - %s\n' "$LAST_ACTIVITY_AGE" "${LAST_ACTIVITY_LINE:-(no tool activity logged)}"
  else
    printf '  activity         : idle (no log output for >%s min)\n' "$RUN_ACTIVE_MINUTES"
  fi
  printf '  log              : %s\n' "$APP_LOG"
}

do_start() {
  local pid; pid="$(server_pid)"
  if [[ -n "$pid" ]]; then
    err "Port ${STREAMLIT_SERVER_PORT} is already in use by pid ${pid}. Use: ./infra/setup.sh restart"
    return 1
  fi
  build_streamlit_cmd "$@" || return 1
  # Rotate rather than truncate: the previous run's log stays readable.
  [[ -f "$APP_LOG" ]] && mv -f "$APP_LOG" "${APP_LOG}.1"
  log "Starting Streamlit detached on $(server_url) (log: ${APP_LOG})"
  nohup "$STREAMLIT_BIN" "${STREAMLIT_ARGS[@]}" >> "$APP_LOG" 2>&1 < /dev/null &
  disown 2>/dev/null || true
  for _ in $(seq 1 60); do
    server_health >/dev/null && break
    sleep 1
  done
  if server_health >/dev/null; then
    log "Running: pid $(server_pid) - $(server_url)"
    show_config
  else
    err "Streamlit did not become healthy within 60s. Last log lines:"
    tail -n 20 "$APP_LOG" >&2
    return 1
  fi
}

do_stop() {
  local force=false
  [[ "${1:-}" == "--force" ]] && force=true
  local pid; pid="$(server_pid)"
  if [[ -z "$pid" ]]; then
    log "Nothing is listening on port ${STREAMLIT_SERVER_PORT}; nothing to stop."
    return 0
  fi
  local sessions reason=""
  sessions="$(browser_sessions)"
  [[ "$sessions" -gt 0 ]] && reason="${sessions} browser session(s) connected"
  if run_looks_active; then
    reason="${reason:+${reason}; }a run looks active (app.log changed ${LAST_ACTIVITY_AGE}s ago: ${LAST_ACTIVITY_LINE:-recent output})"
  fi
  if [[ -n "$reason" && "$force" != true ]]; then
    err "Refusing to stop pid ${pid}: ${reason}."
    err "Stopping kills any upgrade in progress and resets the chat. Re-run with --force to stop anyway."
    return 1
  fi
  [[ -n "$reason" ]] && warn "Stopping pid ${pid} anyway (--force): ${reason}"
  log "Stopping Streamlit (pid ${pid})"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 10); do
    [[ -z "$(server_pid)" ]] && break
    sleep 1
  done
  if [[ -n "$(server_pid)" ]]; then
    warn "Still running after 10s; sending SIGKILL"
    kill -9 "$pid" 2>/dev/null || true
    sleep 1
  fi
  if [[ -z "$(server_pid)" ]]; then
    log "Stopped."
  else
    err "Port ${STREAMLIT_SERVER_PORT} is still busy (pid $(server_pid))."
    return 1
  fi
}

do_restart() {
  local force=()
  [[ "${1:-}" == "--force" ]] && force=(--force)
  do_stop ${force[@]+"${force[@]}"} || return 1
  do_start
}

# ---------------------------------------------------------------------------
# Effective configuration summary
# ---------------------------------------------------------------------------
# Where a value came from: "shell" (exported before the script ran, wins over
# the file), ".env" (set in the file) or "default" (neither; the script's or
# the app's built-in default applies).
src_of() {
  local key="$1"
  if [[ "$_FORGE_SHELL_KEYS" == *" ${key} "* ]]; then
    printf 'shell'
  elif grep -qE "^[[:space:]]*${key}=" "$ENV_FILE" 2>/dev/null; then
    printf '.env'
  else
    printf 'default'
  fi
}

kv() {  # label value [note]
  printf '  %-24s %s%s\n' "$1" "$2" "${3:+  ($3)}"
}

kv_env() {  # KEY [text when empty]: the variable's value with its source
  local key="$1" val="${!1:-}"
  if [[ -n "$val" ]]; then kv "$key" "$val" "$(src_of "$key")"; else kv "$key" "${2:-<unset>}"; fi
}

kv_secret() {  # KEY: never the value, only whether it is set
  local key="$1"
  if [[ -n "${!1:-}" ]]; then kv "$key" "set" "$(src_of "$key")"; else kv "$key" "unset"; fi
}

show_paths() {
  set_paths
  log "Paths exported for this process"
  kv "JAVA_HOME" "${JAVA_HOME:-<unset>}" "${JAVA_HOME_SOURCE:-unset}"
  if [[ -n "${JAVA_HOME:-}" && -x "${JAVA_HOME}/bin/java" ]]; then
    kv "java" "$("${JAVA_HOME}/bin/java" -version 2>&1 | head -1)"
  fi
  kv "PYTHONPATH" "${PYTHONPATH:-<unset>}"
  if [[ -d "$VENV_BIN" ]]; then kv "venv" "$VENV_DIR" "on PATH"; else kv "venv" "$VENV_DIR" "missing - run: ./infra/setup.sh install"; fi
  kv "MAVEN_OPTS" "${MAVEN_OPTS:-}"
  kv "MAVEN_ARGS" "${MAVEN_ARGS:-}"
  kv "AWS_REGION" "$AWS_REGION" "$(src_of AWS_REGION)"
  kv "AWS_DEFAULT_REGION" "${AWS_DEFAULT_REGION:-}"
  kv "FORGE_TOOL_ROOT" "${FORGE_TOOL_ROOT:-}"
}

_FORGE_CONFIG_SHOWN=false
show_config() {
  # Once per invocation: `all` runs install and then run, both of which end here.
  [[ "$_FORGE_CONFIG_SHOWN" == true ]] && return 0
  _FORGE_CONFIG_SHOWN=true
  show_paths
  local prefix="${PARAMETER_STORE_PREFIX:-forge_tool_}"

  log "AWS credentials"
  kv_env AWS_PROFILE "<none - default credential chain>"
  kv_secret AWS_ACCESS_KEY_ID
  kv_secret AWS_SECRET_ACCESS_KEY
  kv_secret AWS_SESSION_TOKEN

  log "Models"
  kv_env BEDROCK_MODEL_ID "<app default>"
  kv_env BEDROCK_MODEL_REGION "<app default: AWS_REGION>"
  kv_env BEDROCK_MAX_TOKENS "<app default>"
  kv_env REVIEWER_ENABLED "<app default: true>"
  kv_env REVIEWER_MODEL_ID "<app default>"
  kv_env REVIEW_SCORE_THRESHOLD "<app default: 7>"

  log "Knowledge base and guardrail"
  kv_env KNOWLEDGE_BASE_ID "<resolved at startup from SSM ${prefix}knowledge_base_id>"
  kv_env KNOWLEDGE_BASE_REGION "<app default: AWS_REGION>"
  kv_env KNOWLEDGE_BASE_DIRECTORY "<app default: ./knowledge-base>"
  kv_env GUARDRAIL_ID "<resolved at startup from SSM ${prefix}guardrail_id>"
  kv_env GUARDRAIL_VERSION "<resolved at startup from SSM ${prefix}guardrail_version>"
  kv_env SECRET_SCAN_ENABLED "<app default: true>"

  log "Git and run defaults"
  kv_env GIT_SOURCE_BRANCH "<repo default branch>"
  kv_env GIT_BASE_BRANCH "<app default: main>"
  kv_env DEFAULT_GITHUB_URL "<none - the app will prompt>"
  kv_env DEFAULT_UPGRADE_DETAILS "<none - the app will prompt>"
  kv "GitHub PAT" "SSM ${prefix}api_key" "SecureString, read into memory at startup"

  log "Agent"
  kv_env AGENT_RECURSION_LIMIT "<app default: 400>"
  kv_env MIGRATION_PLAN_APPROVAL "<app default: true>"
  kv_env MAVEN_OUTPUT_MAX_CHARS "<app default: 6000>"
  kv_env LOG_LEVEL "<app default: DEBUG>"

  log "Server"
  kv "entrypoint" "$APP_ENTRYPOINT"
  kv "url" "$(server_url)"
  kv "headless" "$STREAMLIT_SERVER_HEADLESS"
  kv "log" "$APP_LOG"
  kv ".env" "$ENV_FILE"
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if [[ "$FORGE_SOURCED" == true ]]; then
  show_paths
  log "Exported into this shell (PATH now includes the JDK and the venv)."
else
  command="${1:-all}"
  [[ $# -gt 0 ]] && shift
  case "$command" in
    install)     do_install ;;
    run)         do_run "$@" ;;
    start)       do_start "$@" ;;
    stop)        do_stop "$@" ;;
    restart)     do_restart "$@" ;;
    status)      do_status ;;
    check)       do_check; rc=$?; show_config; exit "$rc" ;;
    config|env)  show_config ;;
    all)         do_install && do_run "$@" ;;
    -h|--help)   awk 'NR > 1 && !/^#/ { exit } NR > 1 { print }' "${_FORGE_SRC}" | sed 's/^# \{0,1\}//' ;;
    *)           err "Unknown command: ${command}"
                 err "Usage: ./infra/setup.sh [install|run|start|stop|restart|status|check|config|all] [app args]"
                 exit 1 ;;
  esac
fi
