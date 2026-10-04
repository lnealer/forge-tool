"""Central configuration for forge-tool.

Every tunable value lives in the repo-root ``.env`` file (see ``.env.example``).
Real environment variables always win over ``.env`` entries, so CI and
containers can override anything without editing files.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

# override=False: an exported env var beats the .env file.
load_dotenv(REPO_ROOT / ".env", override=False)


def env_str(name, default=""):
    """Read a string, treating blank/unset as the default."""
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def env_int(name, default):
    value = env_str(name)
    return int(value) if value else default


def env_float(name, default):
    value = env_str(name)
    return float(value) if value else default


def env_bool(name, default=False):
    value = env_str(name).lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def env_list(name, default=()):
    value = env_str(name)
    if not value:
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


def resolve_path(value):
    """Resolve a possibly relative path against the repo root."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


# --- AWS -------------------------------------------------------------------
AWS_REGION = env_str("AWS_REGION", "us-east-1")

# --- Bedrock model ---------------------------------------------------------
BEDROCK_MODEL_ID = env_str(
    "BEDROCK_MODEL_ID", "global.anthropic.claude-haiku-4-5-20251001-v1:0"
)
BEDROCK_MODEL_REGION = env_str("BEDROCK_MODEL_REGION", AWS_REGION)
BEDROCK_MAX_TOKENS = env_int("BEDROCK_MAX_TOKENS", 10000)
BEDROCK_TEMPERATURE = env_float("BEDROCK_TEMPERATURE", 0.0)
BEDROCK_CONNECT_TIMEOUT = env_int("BEDROCK_CONNECT_TIMEOUT", 240)
BEDROCK_READ_TIMEOUT = env_int("BEDROCK_READ_TIMEOUT", 240)
BEDROCK_MAX_ATTEMPTS = env_int("BEDROCK_MAX_ATTEMPTS", 2)
# Max LangGraph steps (model calls + tool runs) per agent turn. The library
# default of 25 is far too low for a multi-file upgrade.
AGENT_RECURSION_LIMIT = env_int("AGENT_RECURSION_LIMIT", 400)
# Mid-turn context compaction: once the transcript passes TRIGGER tokens, older
# messages are replaced by a summary (the last KEEP messages stay verbatim), so a
# long migration keeps going instead of the model stopping "due to token limits".
AGENT_SUMMARY_ENABLED = env_bool("AGENT_SUMMARY_ENABLED", True)
AGENT_SUMMARY_TRIGGER_TOKENS = env_int("AGENT_SUMMARY_TRIGGER_TOKENS", 120000)
AGENT_SUMMARY_KEEP_MESSAGES = env_int("AGENT_SUMMARY_KEEP_MESSAGES", 30)
# Only the last N characters of a Maven run are returned to the model.
MAVEN_OUTPUT_MAX_CHARS = env_int("MAVEN_OUTPUT_MAX_CHARS", 6000)

# Parent directory of the per-run sandbox (empty = the system temp dir). On
# Windows a short root such as C:\forge-work keeps deep Maven paths under the
# 260-character limit.
WORK_ROOT = env_str("WORK_ROOT")

# --- Knowledge base --------------------------------------------------------
KNOWLEDGE_BASE_ID = env_str("KNOWLEDGE_BASE_ID")
KNOWLEDGE_BASE_REGION = env_str("KNOWLEDGE_BASE_REGION", AWS_REGION)
KNOWLEDGE_BASE_NUM_RESULTS = env_int("KNOWLEDGE_BASE_NUM_RESULTS", 4)
KNOWLEDGE_BASE_DIRECTORY = env_str("KNOWLEDGE_BASE_DIRECTORY", "./knowledge-base")

# --- SSM Parameter Store ---------------------------------------------------
PARAMETER_STORE_PREFIX = env_str("PARAMETER_STORE_PREFIX", "forge_tool_")
SSM_PARAMETER_NAMES = env_list("SSM_PARAMETER_NAMES", ("api_key",))
# Written by infra/deploy.sh; used when KNOWLEDGE_BASE_ID is not set.
KNOWLEDGE_BASE_ID_PARAMETER = f"{PARAMETER_STORE_PREFIX}knowledge_base_id"

# --- Git / GitHub ----------------------------------------------------------
GIT_COMMIT_AUTHOR_NAME = env_str("GIT_COMMIT_AUTHOR_NAME", "upgrade-code-bot")
GIT_COMMIT_AUTHOR_EMAIL = env_str("GIT_COMMIT_AUTHOR_EMAIL", "upgrade@code.bot")
GIT_SOURCE_BRANCH = env_str("GIT_SOURCE_BRANCH")
GIT_BASE_BRANCH = env_str("GIT_BASE_BRANCH", "main")
GITHUB_API_TIMEOUT = env_int("GITHUB_API_TIMEOUT", 30)
GITHUB_API_VERSION = env_str("GITHUB_API_VERSION", "2022-11-28")

# --- Upgrade run defaults --------------------------------------------------
DEFAULT_GITHUB_URL = env_str("DEFAULT_GITHUB_URL")
DEFAULT_UPGRADE_DETAILS = env_str("DEFAULT_UPGRADE_DETAILS")

# --- Streamlit app ---------------------------------------------------------
APP_PAGE_TITLE = env_str("APP_PAGE_TITLE", "Forge Chatbot")
APP_PAGE_ICON = env_str("APP_PAGE_ICON", "🤖")
APP_CAPTION = env_str("APP_CAPTION", "Powered by AWS Bedrock & Streamlit")

# --- Logging ---------------------------------------------------------------
LOG_NAME = env_str("LOG_NAME", "FORGE_TOOL")
LOG_LEVEL = env_str("LOG_LEVEL", "DEBUG")
ROOT_LOG_LEVEL = env_str("ROOT_LOG_LEVEL", "INFO")

# --- Guardrails ------------------------------------------------------------
GUARDRAIL_ID = env_str("GUARDRAIL_ID")
GUARDRAIL_VERSION = env_str("GUARDRAIL_VERSION")
GUARDRAIL_TRACE = env_bool("GUARDRAIL_TRACE", False)
GUARDRAIL_ID_PARAMETER = f"{PARAMETER_STORE_PREFIX}guardrail_id"
GUARDRAIL_VERSION_PARAMETER = f"{PARAMETER_STORE_PREFIX}guardrail_version"
SECRET_SCAN_ENABLED = env_bool("SECRET_SCAN_ENABLED", True)
SECRET_SCAN_ALLOWLIST = env_str("SECRET_SCAN_ALLOWLIST")

# Repository personal-data scan with Amazon Bedrock (report only, see pii_scan.py).
PII_SCAN_ENABLED = env_bool("PII_SCAN_ENABLED", True)
PII_SCAN_GUARDRAIL_ID = env_str("PII_SCAN_GUARDRAIL_ID")
PII_SCAN_GUARDRAIL_VERSION = env_str("PII_SCAN_GUARDRAIL_VERSION")
PII_SCAN_GLOBS = env_list("PII_SCAN_GLOBS", (
    "**/*.sql", "**/*.csv", "**/*.json", "**/*.properties", "**/*.yml", "**/*.yaml", "**/*.txt",
    "**/src/test/resources/**", "**/src/main/resources/**", "**/src/test/java/**/*.java",
))
PII_SCAN_CHUNK_CHARS = env_int("PII_SCAN_CHUNK_CHARS", 10000)
PII_SCAN_MAX_CHARS = env_int("PII_SCAN_MAX_CHARS", 1500000)

# --- Reviewer model --------------------------------------------------------
REVIEWER_ENABLED = env_bool("REVIEWER_ENABLED", True)
REVIEWER_MODEL_ID = env_str("REVIEWER_MODEL_ID", "us.amazon.nova-pro-v1:0")
REVIEWER_MODEL_REGION = env_str("REVIEWER_MODEL_REGION", AWS_REGION)
REVIEWER_MAX_TOKENS = env_int("REVIEWER_MAX_TOKENS", 2000)
REVIEW_SCORE_THRESHOLD = env_int("REVIEW_SCORE_THRESHOLD", 7)
REVIEW_MAX_DIFF_CHARS = env_int("REVIEW_MAX_DIFF_CHARS", 60000)

# --- Pull request gate -----------------------------------------------------
# block: refuse create_pull_request until the final tests compile with no new
#        failures, approved packs are clean (or their leftovers are named in the PR)
#        and test parity holds (or the user approved the file);
# draft: open the PR as a GitHub draft with the failures listed on top;
# off:   no gate.
PR_GATE = env_str("PR_GATE", "block").lower()

# --- Migration plan --------------------------------------------------------
# True: the agent proposes a plan and waits for approval before editing anything.
MIGRATION_PLAN_APPROVAL = env_bool("MIGRATION_PLAN_APPROVAL", True)
