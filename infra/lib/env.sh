# shellcheck shell=bash
#
# Shared .env loader for the scripts in infra/.
#
# Values already present in the environment are left alone, so an exported
# variable beats the file. That matches load_dotenv(override=False) in
# python/settings.py, which keeps the shell and the app reading configuration
# the same way.

forge_load_env() {
  local env_file="$1" line key value
  [[ -f "$env_file" ]] || return 1

  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"          # strip leading space
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" == *=* ]] || continue

    key="${line%%=*}"
    key="${key%"${key##*[![:space:]]}"}"             # strip trailing space
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue

    value="${line#*=}"
    # Strip one layer of matching quotes, as python-dotenv does.
    if [[ ${#value} -ge 2 && "$value" == \"*\" ]]; then
      value="${value:1:${#value}-2}"
    elif [[ ${#value} -ge 2 && "$value" == \'*\' ]]; then
      value="${value:1:${#value}-2}"
    fi

    [[ -z "${!key:-}" ]] && export "$key=$value"
  done < "$env_file"
  return 0
}
