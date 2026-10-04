import boto3
from botocore.client import Config
from langchain_aws import ChatBedrock
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from langchain.agents import create_agent
import git_utils
from git_utils import clone_repo, create_branch, git_commit, git_restore_file, git_status
from typing import List
from langchain_core.prompts import PromptTemplate
from langchain_community.agent_toolkits import FileManagementToolkit
import subprocess
import threading
from datetime import datetime
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_aws.retrievers import AmazonKnowledgeBasesRetriever
import streamlit as st
import asyncio
import os

from langchain_community.tools.file_management.write import WriteFileTool

import json
import re

import guardrails
import pii_scan
import settings
import ui_events
import workdir
import techstack
from techstack import detect_tech_stack
from reviewer import review_migrated_files
from utils import get_guardrail_config, get_logger, get_knowledge_base_id

config = Config(
    connect_timeout=settings.BEDROCK_CONNECT_TIMEOUT,
    read_timeout=settings.BEDROCK_READ_TIMEOUT,
    retries={
        'total_max_attempts': settings.BEDROCK_MAX_ATTEMPTS,
        'max_attempts': settings.BEDROCK_MAX_ATTEMPTS,
    },
)
# config = {"recursion_limit": 10}


logger = get_logger()

DEFAULT_MODEL = settings.BEDROCK_MODEL_ID
DEFAULT_MODEL_REGION = settings.BEDROCK_MODEL_REGION
PROMPT_TEMPLATE = """
You are a code migration assistant for J2EE applications: Java, Spring, Spring Boot, Struts, JSF, EJB, JPA/Hibernate, JMS and related stacks.
Work in phases and keep the user informed with short messages.

Phase 1 - Prepare. Clone the repo with clone_repo(url=repo_url, repo_dir="repo", branch=source_branch).
Every path you pass to any tool is relative to tmpdir (or absolute under it): the clone is at repo/, so list repo/, read repo/pom.xml, run maven on repo/<module>, and pass repo for create_branch and git_commit. Never use paths outside tmpdir.
Then run scan_for_secrets on repo and note any findings. Establish the test baseline before changing anything: run_maven_install on each parent/BOM pom and then on each reactor in build order (detect_tech_stack lists them under build.parents and build.reactors; the repo's build script shows the order, and a reactor's modules may depend on an earlier reactor's installed artifacts), then run_maven_test(baseline=true) on each reactor. Run Maven calls one at a time, never in parallel. Failures recorded in the baseline predate the migration; they are reported in the PR, never fixed unless a plan item covers them. Never copy, print or move secret values. If it found secrets, the plan in Phase 3 must include the pack externalize-secrets as in_scope=true (always; risk high; note 'rotate after externalizing'). If it found public keys, stop and wait for the user's decision before the baseline; include externalize-public-keys as in_scope only if the user chose to scrub them.

Phase 2 - Discover. Run detect_tech_stack on repo. If repo/TECH_STACK.md or a README exists, read it for context, but documents go stale: trust detect_tech_stack for versions and for what the code actually uses (the BOM can be ahead of the sources, and the app server config can be behind both).
The result's packs.applicable lists, in dependency order, the guideline packs whose own detect rules matched, with evidence; packs.gated lists packs that need a decision (e.g. container=tomcat). Run list_guideline_packs for titles and tiers. For each applicable pack query code_upgrade_knowledge_base (when available) for its transform guidance. A pack with status detect-only has no transform guidance yet: list it as an item with in_scope=false and note that it needs a manual migration.

Phase 3 - Plan. Call list_migration_files(repo_dir="repo", pack_ids=<the applicable pack ids, comma-separated>) to size each item from the packs' own file selectors. Then call propose_migration_plan with one item per applicable pack (use the pack id as pack), following the dependency order: its current version or state, the target the pack prescribes, the pack name, the scope taken from the inventory (MUST CHANGE count and modules, e.g. '7 files in AssetManagementInternalWeb'), a risk level and notes. Mark in_scope=true for the items needed to reach the upgrade goal (and anything those require); list the other candidates with in_scope=false so the user can opt in. Some packs match on usage alone (imports or config files) even when detect_tech_stack shows the component already at the pack's target version: plan those as in_scope=false verification items with the note 'already at target; verify only', not as migrations. Components with no applicable pack and nothing to change are not items; mention them in the summary. Then stop: give the user a short, readable version of the plan and wait for approval. Do not change any file before the plan is approved.

Phase 4 - Migrate. After approval, work from the queue: call next_migration_files(repo_dir="repo"), migrate every file it returns, each exactly once (read it, apply the transform guidance of each pack listed for it, left to right, write it once), then call next_migration_files again; repeat until it says no files are left. The queue puts main code before tests; do not reorder it. Approved plan items are never descoped, deferred or marked out of scope by you: if a file cannot be migrated, stop and ask the user - only the user can accept leftovers. Leave VERIFY ONLY files alone unless a pack's guidance names them. In test files change only the JUnit/Mockito syntax, never what a test checks: keep every test method, assertion and expected value; run check_test_parity after editing tests. Then run check_pack_acceptance(repo_dir="repo") and migrate every leftover. Modify only code relevant to the approved items. If a turn stops at the step limit, the user will say 'continue': call git_status on repo and resume from where the working copy stands. Never stop because of token or context limits: the tooling compacts the conversation history automatically. Keep working through the queue until it is empty and check_pack_acceptance is clean, and read a file only when you are about to edit it. Every hardcoded secret is replaced with an environment lookup named for its purpose (for example System.getenv("AES_KEY") in Java, ${{DB_PASSWORD}} in configuration), following the externalize-secrets guidance; never carry or copy the literal, and keep a list of every variable you introduce. Then run the tests the same way as the baseline: run_maven_install on the parent/BOM poms and the reactors in build order, then run_maven_test per reactor (not per module), one Maven call at a time. Each result compares against the baseline: fix the new failures; report the pre-existing ones. Then commit and push with create_branch(repo_file_path="repo", commit_message=...) leaving branch_name empty: the tool names the branch forge-upgrade-<timestamp> (or reuses the current forge-upgrade branch) and tells you the name to use for the PR. Use git_commit for follow-up pushes.

Phase 5 - Review and PR. First run check_pack_acceptance and check_test_parity once more, and run run_maven_test on every reactor at the final commit; then call review_migrated_files so the reviewer model scores every changed file. If any file is flagged for manual review, stop: tell the user which files were flagged and why, and wait for their decision before pushing or opening a PR. The user decides per file: approved means keep it as-is; rejected means restore it with git_restore_file and leave it out; retry means revise it to address the reviewer's issues and run review_migrated_files on that file again.
Once review passes or the user approves, push the branch and create a PR with create_pull_request, passing repo_url as github_url; it targets base_branch. create_pull_request runs a gate on tool results (tests at the current commit compile with no new failures, every approved pack clean unless the user accepted its leftovers, test parity) and appends a generated verification section; if it refuses, fix what it lists. Never call a failure pre-existing unless run_maven_test's baseline comparison says so. The PR description must include the detected stack summary, the migration plan as a table, the test results compared with the baseline (new failures fixed, pre-existing failures listed), the review scores, any secret-scan findings, a 'Detected but not migrated' section (only optional items, detect-only packs and leftovers the user accepted, with the reason, plus manual checks such as authz_parity), and a 'Configuration required' section listing every environment placeholder you introduced (and that the original values should be rotated); report remaining issues.

Respond with a message when you're ready for a review or when you have a question.
Never include credentials, keys or tokens in messages, commits or the PR.
When the user asks for changes, make the relevant changes to the code and then push to the remote for review.
Push additional changes to your original branch.

<upgrade_goal>
{version}
</upgrade_goal>
<repo_url>
{repo_url}
</repo_url>
<repo_api_url>
{repo_api_url}
</repo_api_url>
<source_branch>
{source_branch}
</source_branch>
<base_branch>
{base_branch}
</base_branch>
<tmpdir>
{tmpdir}
</tmpdir>
"""


class Model:
    """Model class for GenAI."""

    def _config(self):
        return {"recursion_limit": settings.AGENT_RECURSION_LIMIT}

    def invoke(self, conversation):
        return self.llm.invoke({"messages": conversation}, config=self._config())

    def stream(self, conversation):
        """Yield the full message list after every graph step (model call or tool run).

        stream_mode="values" emits the whole state each step, so the caller can
        diff consecutive lists to see what just happened and render it live.
        """
        yield from self.llm.stream(
            {"messages": conversation}, config=self._config(), stream_mode="values"
        )

class Claude(Model):
    """Claude model class."""

    def __init__(self, model_id=DEFAULT_MODEL, model_aws_region=DEFAULT_MODEL_REGION, working_dir=""):
        logger.info(f"Initializing Claude with model_id: {model_id} and region: {model_aws_region}")
        bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=model_aws_region,
            config=config
        )

        # Bedrock guardrail (PII masking, secret-pattern blocking) on every call.
        guardrail = get_guardrail_config()
        llm_kwargs = {"guardrails": guardrail} if guardrail else {}
        logger.info(f"Bedrock guardrail: {'on' if guardrail else 'off'}")

        # Create agent with tools
        unstructured_llm = ChatBedrock(
            client=bedrock_client,
            region_name = model_aws_region,
            model_id=model_id,
            max_tokens=settings.BEDROCK_MAX_TOKENS,
            **llm_kwargs,
            model_kwargs={
                "temperature": settings.BEDROCK_TEMPERATURE,
                "max_tokens": settings.BEDROCK_MAX_TOKENS,
                # "top_p": 0.999,
                # "top_k": 250,
                # "stop_sequences": [
                #     "\\n\\nHuman::",
                # ],
            },
        )

        # write_file is replaced by a guarded version that refuses secret material.
        toolkit = FileManagementToolkit(root_dir=working_dir, selected_tools=["read_file", "list_directory"])
        tools = [
            run_maven_test,
            run_maven_install,
            create_pull_request,
            clone_repo,
            create_branch,
            get_current_timestamp,
            git_commit,
            git_restore_file,
            git_status,
            scan_for_secrets,
            detect_tech_stack,
            list_guideline_packs,
            propose_migration_plan,
            list_migration_files,
            check_pack_acceptance,
            check_test_parity,
            next_migration_files,
            make_guarded_write_tool(working_dir),
        ] + toolkit.get_tools()
        if settings.REVIEWER_ENABLED:
            tools.append(review_migrated_files)

        # The knowledge base is optional: without one the agent still upgrades
        # code, it just has no company-guidelines tool. A missing knowledge base
        # used to raise here and leave the chat unable to render at all.
        kb_tool = load_kb_tool()
        if kb_tool is not None:
            tools.append(kb_tool)

        llm = unstructured_llm.bind_tools(tools)
        self.llm = create_agent(model=llm, tools=tools, middleware=_context_middleware(unstructured_llm))
        logger.info(f"Initialized Claude with {len(tools)} tools")

    def create_prompt(self, version, repo_api_url, repo_url, tmpdir):
        """Create a prompt for the model to generate a code upgrade."""
        logger.info("Creating prompt for model")
        prompt = PROMPT_TEMPLATE.format(
            version=version,
            repo_api_url=repo_api_url,
            repo_url=repo_url,
            tmpdir=tmpdir,
            source_branch=settings.GIT_SOURCE_BRANCH or "<repo default>",
            base_branch=settings.GIT_BASE_BRANCH,
        )
        return prompt


    def _invoke(self, prompt):
        """Invoke the model with the prompt."""
        response = self.llm.invoke(prompt)
        # Append opening curly braces which might be missing, depending on the prompt.
        logger.info(f"Raw response from GenAI: {response}")
        # if not response.startswith("{"):
        #     response = "{" + response
        return response
    
    def _invoke_unstructured(self, prompt):
        """Invoke the model with the prompt."""
        response = self.unstructured_llm.invoke(prompt)
        logger.info(f"Raw response from GenAI: {response}")
        return response

def _context_middleware(summary_model):
    """Summarize older history once a turn's transcript gets large.

    Without it the transcript only grows (whole files read and written, Maven
    tails), and Claude Haiku 4.5, which is told its remaining context, ends the
    turn early with a "still to do" list. The summary replaces older messages;
    the last AGENT_SUMMARY_KEEP_MESSAGES stay verbatim, and tool calls are never
    split from their results by the middleware.
    """
    if not settings.AGENT_SUMMARY_ENABLED:
        return []
    try:
        from langchain.agents.middleware import SummarizationMiddleware
    except ImportError as e:
        logger.warning(f"Context compaction unavailable ({e}); long turns may stop early.")
        return []
    logger.info(f"Context compaction on: summarize past {settings.AGENT_SUMMARY_TRIGGER_TOKENS} tokens, "
                f"keep the last {settings.AGENT_SUMMARY_KEEP_MESSAGES} messages")
    return [SummarizationMiddleware(
        model=summary_model,
        trigger=("tokens", settings.AGENT_SUMMARY_TRIGGER_TOKENS),
        keep=("messages", settings.AGENT_SUMMARY_KEEP_MESSAGES),
    )]

# TODO:
# Add chat tool for getting user input
# Add second reviewer agent
# Add test harness tools   
# 
def load_kb_tool():
    """Build the knowledge base retriever tool, or None if no KB is configured."""
    try:
        kb_id = get_knowledge_base_id()
    except Exception as e:
        logger.warning(
            f"No knowledge base available ({e}); continuing without the "
            "code_upgrade_knowledge_base tool. Run ./infra/deploy.sh --sync to create one."
        )
        return None

    logger.info(f"Loading knowledge base tool for kb_id={kb_id}")
    # A VECTOR knowledge base (ours: S3 Vectors) takes vectorSearchConfiguration;
    # managedSearchConfiguration is only valid for Bedrock managed knowledge bases
    # and makes Retrieve fail with a ValidationException.
    retriever = AmazonKnowledgeBasesRetriever(
        knowledge_base_id=kb_id,
        region_name=settings.KNOWLEDGE_BASE_REGION,
        retrieval_config={
            "vectorSearchConfiguration": {
                "numberOfResults": settings.KNOWLEDGE_BASE_NUM_RESULTS
            }
        },
    )

    @tool
    def code_upgrade_knowledge_base(query: str) -> str:
        """Search the company coding guidelines for standards relevant to a change.

        Args:
            query: What you are about to change, e.g. "javax to jakarta namespace migration".
        """
        # The knowledge base is optional, so a failed lookup is reported back to
        # the model as a result instead of raising; an exception here would
        # otherwise escape the agent loop and abort the whole turn.
        try:
            docs = retriever.invoke(query)
        except Exception as e:
            logger.warning(f"Knowledge base lookup failed: {e}")
            return (
                f"ERROR: knowledge base lookup failed: {e}. "
                "Continue without company guidelines for this change."
            )
        if not docs:
            return "No guidelines found for that query."
        chunks = []
        for doc in docs:
            uri = (doc.metadata.get("location") or {}).get("s3Location", {}).get("uri", "")
            header = f"[source: {uri}]\n" if uri else ""
            chunks.append(header + doc.page_content)
        return "\n\n".join(chunks)

    return code_upgrade_knowledge_base
 
# Module totals only (per-class lines carry "Time elapsed"; the module summary line does not).
_SUREFIRE_TOTAL = re.compile(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)\s*$", re.M)
# "[ERROR] pkg.Class.method -- Time elapsed: 0.1 s <<< FAILURE!" (surefire 3) or
# "[ERROR] method(pkg.Class)  Time elapsed: 0.1 s  <<< ERROR!" (surefire 2).
_SUREFIRE_FAIL = re.compile(
    r"^\[ERROR\]\s+(\S+?)(?:\(([A-Za-z_][\w$]*(?:\.[\w$]+)+)\))?\s+(?:--\s+)?Time elapsed.*<<< (?:FAILURE|ERROR)!", re.M)
# Surefire's closing summary: "[ERROR]   Class.method:42 » IllegalState ..."
_SUREFIRE_SUMMARY = re.compile(r"^\[ERROR\]\s+([A-Za-z_][\w$]*\.[\w$]+(?:\.[\w$]+)*):\d+", re.M)
_COMPILE_ERROR = re.compile(r"^\[ERROR\] (\S+?\.java):\[(\d+),\d+\] (.*)$", re.M)
# "[ERROR] 'dependencies.dependency.version' for x:y:jar is missing. @ line 135, column 17"
_POM_ERROR = re.compile(r"^\[ERROR\] ('.*?@ line \d+, column \d+)", re.M)
_REACTOR_LINE = re.compile(r"^\[INFO\] (\S[^.\n]*?) \.{3,} (SUCCESS|FAILURE|SKIPPED)", re.M)

# Test results per resolved directory; baseline=True runs populate it so later
# runs can tell a regression from a failure that predates the migration.
_TEST_BASELINE = {}

# The model may issue several Maven tool calls at once and langgraph runs them
# in parallel threads. Builds share ~/.m2 and file-based test databases (H2
# allows one JVM per file), so parallel runs fail for reasons unrelated to
# the code. One build at a time.
_MAVEN_LOCK = threading.Lock()

# On Windows Maven is mvn.cmd, which a list argv without a shell only finds by its
# full name; shutil.which resolves it (and plain "mvn" elsewhere).
import shutil
_MVN = shutil.which("mvn") or "mvn"

# Latest non-baseline test run per directory, with the repository state it ran
# against. The PR gate requires a passing run at the CURRENT state, so a model's
# "all tests pass" is never taken on trust.
_LAST_TEST = {}


def _repo_state(path):
    """(toplevel, state) where state identifies HEAD plus any uncommitted change."""
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except Exception:
        return None, None
    if top.returncode != 0:
        return None, None
    root = os.path.realpath(top.stdout.strip())
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    import hashlib
    return root, hashlib.sha1((head + "\n" + dirty).encode()).hexdigest()[:16]


def _test_id(name, cls=None):
    """Normalize a failing test to Class.method so every surefire format agrees."""
    if cls:
        return f"{cls.split('.')[-1]}.{name}"
    head = re.split(r"[(\[]", name, 1)[0]
    parts = head.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else head


def _parse_surefire(output):
    counts = None
    totals = _SUREFIRE_TOTAL.findall(output)
    if totals:
        sums = [sum(int(t[i]) for t in totals) for i in range(4)]
        counts = {"run": sums[0], "failures": sums[1], "errors": sums[2], "skipped": sums[3]}
    failing = {_test_id(m.group(1), m.group(2)) for m in _SUREFIRE_FAIL.finditer(output)}
    failing |= {_test_id(m.group(1)) for m in _SUREFIRE_SUMMARY.finditer(output)}
    compile_errors = [f"{os.path.basename(f)}:{line} {msg.strip()}" for f, line, msg in _COMPILE_ERROR.findall(output)]
    compile_errors += [f"pom: {m}" for m in dict.fromkeys(_POM_ERROR.findall(output))]  # deduped, in order
    reactor = [f"{name.strip()}: {state}" for name, state in _REACTOR_LINE.findall(output)]
    return counts, sorted(failing), compile_errors, reactor


def _run_maven(code_dir, goals, label, baseline=False):
    """Run `mvn <goals> -f <code_dir>/pom.xml` and return a compact, structured result.

    argv list, no shell: code_dir comes from the model, so it must never be
    interpolated into a shell command. The head of the result is a parsed
    summary (counts, failing tests, reactor modules, comparison with the test
    baseline); only the tail of the raw log follows, so a long build does not
    flood the model's context.
    """
    try:
        code_dir = workdir.resolve(code_dir)
    except ValueError as e:
        return f"ERROR: {e}"
    if not os.path.isdir(code_dir):
        return f"ERROR: {code_dir} is not a directory."
    pom = os.path.join(code_dir, "pom.xml")
    if not os.path.isfile(pom):
        return f"ERROR: no pom.xml in {code_dir}. Pass the directory that contains the pom."
    cmd = [_MVN, "-B", "-U", "-f", pom, *goals]
    logger.info(f"Running: {' '.join(cmd)}")
    with _MAVEN_LOCK:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    output = (result.stdout or "") + (result.stderr or "")
    status = "SUCCESS" if result.returncode == 0 else f"FAILED (exit {result.returncode})"
    logger.info(f"Maven {label} in {code_dir}: {status}")

    counts, failing, compile_errors, reactor = _parse_surefire(output)
    lines = [f"mvn {' '.join(goals)} in {code_dir}: {status}"]
    if compile_errors:
        lines.append(f"compile errors: {len(compile_errors)}")
        lines += [f"  {e}" for e in compile_errors[:15]]
        if len(compile_errors) > 15:
            lines.append(f"  ... {len(compile_errors) - 15} more")
    if counts:
        lines.append(f"tests: run {counts['run']}, failures {counts['failures']}, errors {counts['errors']}, skipped {counts['skipped']}")
    if failing:
        lines.append("failing tests: " + ", ".join(failing[:25]) + (f" (+{len(failing) - 25} more)" if len(failing) > 25 else ""))
    if reactor:
        lines.append("reactor: " + "; ".join(reactor[:20]))
    if "test" in goals and counts is None:
        lines.append("no tests ran: the build failed before the test phase (pom or compile errors above / in the log tail)")
    if "test" in goals:
        if baseline:
            _TEST_BASELINE[code_dir] = {"failing": set(failing), "reached_tests": counts is not None}
            lines.append(f"baseline recorded: {len(failing)} failing test(s) BEFORE the migration"
                         if counts is not None else
                         "baseline recorded: the pre-migration build does not reach the tests; after the migration every failure counts as new")
        elif code_dir in _TEST_BASELINE:
            entry = _TEST_BASELINE[code_dir]
            base = entry["failing"]
            new = sorted(set(failing) - base)
            old = sorted(set(failing) & base)
            fixed = sorted(base - set(failing))
            if not entry["reached_tests"]:
                lines.append("vs baseline: the pre-migration build did not reach the tests, so nothing here is known to be pre-existing")
            lines.append(f"vs baseline: {len(new)} new failure(s)" + (": " + ", ".join(new[:15]) if new else "")
                         + f"; {len(old)} pre-existing (failed before the migration too)" + (": " + ", ".join(old[:15]) if old else "")
                         + (f"; {len(fixed)} fixed" if fixed else ""))
            if new:
                lines.append("ACTION: the new failures are regressions from your changes - fix them. Pre-existing failures are reported, not fixed.")
            elif failing:
                lines.append("ACTION: no regressions; the failures above predate the migration - report them in the PR, do not try to fix them.")
        else:
            lines.append("note: no baseline for this directory (run with baseline=true before migrating to tell regressions from pre-existing failures)")
    if "test" in goals and not baseline:
        entry = _TEST_BASELINE.get(code_dir)
        if entry is None or not entry["reached_tests"]:
            new_failures = sorted(failing)
        else:
            new_failures = sorted(set(failing) - entry["failing"])
        root, state = _repo_state(code_dir)
        _LAST_TEST[code_dir] = {
            "compiled": counts is not None and not compile_errors,
            "compile_errors": len(compile_errors), "counts": counts, "failing": sorted(failing),
            "new_failures": new_failures, "exit": result.returncode, "root": root, "state": state,
        }
    tail_size = settings.MAVEN_OUTPUT_MAX_CHARS
    tail = output[-tail_size:]
    return "\n".join(lines) + "\n--- log tail ---\n" + tail


@tool
def run_maven_test(code_dir: str, baseline: bool = False) -> str:
    """Run `mvn clean test` for a reactor or module and return a structured result.

    Prefer the reactor directory (the pom with <modules>): one call builds and
    tests every module in order and resolves sibling dependencies without an
    install. Run with baseline=true once BEFORE changing anything so later runs
    report new failures separately from pre-existing ones.

    Args:
        code_dir: Directory containing the pom.xml (reactor or module), relative to the working directory.
        baseline: True to record this run's failures as the pre-migration baseline.
    """
    return _run_maven(code_dir, ["clean", "test"], "test", baseline=baseline)


@tool
def run_maven_install(code_dir: str) -> str:
    """Run `mvn clean install -DskipTests` for a module so sibling modules can resolve it.

    In multi-reactor builds (e.g. a parent BOM, a common library, then the app)
    install the parent and shared modules first, in build order, before running
    tests on the modules that depend on them.

    Args:
        code_dir: Directory containing the module's pom.xml.
    """
    return _run_maven(code_dir, ["clean", "install", "-DskipTests"], "install")

def make_guarded_write_tool(working_dir):
    """write_file scoped to the working dir that refuses content containing secrets."""
    writer = WriteFileTool(root_dir=working_dir)

    @tool
    def write_file(file_path: str, text: str, append: bool = False) -> str:
        """Write text to a file inside the working directory.

        Refused if the text contains secret material (private keys, AES or
        symmetric keys, AWS keys, tokens, hardcoded passwords).

        Args:
            file_path: Path of the file, relative to the working directory.
            text: Full text to write.
            append: Append instead of overwrite.
        """
        findings = guardrails.scan_text(text, file_path, severities=("secret",))
        if findings:
            ui_events.push_secret_block("write refused", file_path, findings)
            return (
                "ERROR: write refused by the secret guardrail; the content contains what "
                "looks like secret material. Keep secrets out of source files:\n"
                + guardrails.format_findings(findings)
            )
        return writer.invoke({"file_path": file_path, "text": text, "append": append})

    return write_file


@tool
def scan_for_secrets(path: str) -> str:
    """Scan a directory or file for secrets and public keys; values are always redacted.

    Secrets: private keys (SSH/PEM/PGP), AES and other symmetric keys (constants,
    SecretKeySpec literals, byte-array keys), AWS keys, GitHub/Slack tokens, JWTs,
    API keys and hardcoded passwords. These are ALWAYS externalized by the
    migration (pack externalize-secrets). Public keys (PEM or base64 X.509) are
    not secret: the user decides whether to scrub them. Run it on the cloned repo
    right after cloning.

    Args:
        path: Path of a directory or file inside the working directory (e.g. "repo").
    """
    try:
        path = workdir.resolve(path)
    except ValueError as e:
        return f"ERROR: {e}"
    if os.path.isdir(path):
        findings = guardrails.scan_tree(path)
    elif os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as handle:
            findings = guardrails.scan_text(handle.read(), path)
    else:
        return f"ERROR: {path} does not exist."
    secrets = [f for f in findings if f.severity == "secret"]
    public = [f for f in findings if f.severity == "public"]
    pii = pii_scan.scan_tree(path) if os.path.isdir(path) else None
    if not findings and not (pii and pii["findings"]):
        note = f" Bedrock personal-data scan: {pii['error']}." if pii and pii["error"] else ""
        return f"No secrets, public keys or personal data detected under {path}.{note}"
    parts = []
    if secrets:
        ui_events.push_secret_block("found in repository", path, secrets)
        parts.append(
            f"{len(secrets)} hardcoded secret(s) found (values redacted):\n"
            f"{guardrails.format_findings(secrets)}\n"
            "Never copy, print or move these values. They will be externalized: the plan must "
            "include the pack 'externalize-secrets' as IN SCOPE (always), which replaces each literal "
            "with an environment lookup (e.g. System.getenv(\"AES_KEY\") / ${AES_KEY}); list every "
            "variable under 'Configuration required' in the PR and say the originals must be rotated. "
            "The commit gate refuses any diff that adds a literal."
        )
    if public:
        ui_events.push_secret_block("public key found", path, public)
        parts.append(
            f"{len(public)} embedded public key(s) found (shown redacted):\n"
            f"{guardrails.format_findings(public)}\n"
            "Public keys are not secret, so the user decides. STOP now: tell the user how many public "
            "keys were found and in which files, and wait for their decision (the chat shows "
            "'Scrub public keys' and 'Continue (keep them)' buttons). If they choose scrub, add the "
            "pack 'externalize-public-keys' to the plan as in scope; if they keep them, list them in "
            "the PR under 'Detected but not migrated'."
        )
    if pii and pii["findings"]:
        found = pii["findings"]
        ui_events.push_secret_block("personal data found", path, found[:200])
        listed = "\n".join(f"  - {f.path}:{f.line}: {f.kind.split(': ', 1)[-1]}" for f in found[:40])
        more = f"\n  ... {len(found) - 40} more" if len(found) > 40 else ""
        parts.append(
            f"{len(found)} personal data item(s) found by Amazon Bedrock in {len({f.path for f in found})} file(s) "
            f"(values never shown): {pii_scan.summary_by_type(found)}\n{listed}{more}\n"
            "Report these in the PR (the verification section lists them); never copy the values. Replacing them "
            "with synthetic data is not part of this run unless the user asks."
            + (f"\nThe scan stopped at its budget of {settings.PII_SCAN_MAX_CHARS} characters." if pii["truncated"] else "")
        )
    elif pii and pii["error"]:
        parts.append(f"Bedrock personal-data scan: {pii['error']}.")
    return "\n\n".join(parts)


@tool
def list_guideline_packs() -> str:
    """List the company's migration guideline packs: which migrations have guidance.

    One line per pack: id, tier, title (from -> to), what it depends on, and
    the decisions it needs (e.g. container=tomcat). detect_tech_stack already
    reports which packs apply to the repo; use the ids in propose_migration_plan
    and query code_upgrade_knowledge_base for a pack's transform guidance.
    """
    packs = techstack.load_packs()
    if not packs:
        return f"No guideline packs found in {settings.resolve_path(settings.KNOWLEDGE_BASE_DIRECTORY)}."
    lines = []
    for pack in packs:
        extra = []
        if pack.get("depends_on"):
            extra.append("after: " + ", ".join(pack["depends_on"]))
        if pack.get("decisions"):
            extra.append("decisions: " + ", ".join(pack["decisions"]))
        if pack.get("status"):
            extra.append(f"status: {pack['status']}")
        lines.append(f"- {pack['id']} [{pack.get('tier', '?')}]: {pack.get('title', '')}" + (f"  ({'; '.join(extra)})" if extra else ""))
    return f"{len(lines)} guideline pack(s):\n" + "\n".join(lines)


_RISKS = ("low", "medium", "high")


@tool
def propose_migration_plan(plan_json: str) -> str:
    """Record the migration plan and show it to the user for approval.

    plan_json is a JSON object:
    {"goal": "<the upgrade goal>", "summary": "<2-4 sentences incl. components already at target>",
     "items": [{"component": "Struts", "current": "2.5.30", "target": "7.x per struts2-modernize",
                "pack": "struts2-modernize", "scope": "ams-internal/* (12 actions, struts.xml)",
                "in_scope": true, "risk": "medium", "notes": "..."}]}
    in_scope=false marks optional candidates the user can opt into. After calling
    this, stop and wait for the user's approval before editing any file.

    Args:
        plan_json: The plan as a JSON string in the shape above.
    """
    try:
        plan = json.loads(plan_json)
    except (TypeError, ValueError) as e:
        return f"ERROR: plan_json is not valid JSON ({e}). Send a JSON object with goal, summary and items."
    if not isinstance(plan, dict) or not isinstance(plan.get("items"), list) or not plan["items"]:
        return "ERROR: plan must be an object with a non-empty 'items' list."
    items = []
    for i, raw in enumerate(plan["items"]):
        if not isinstance(raw, dict):
            return f"ERROR: item {i} is not an object."
        item = {k: str(raw.get(k, "")).strip() for k in ("component", "current", "target", "pack", "scope", "notes")}
        if not item["component"] or not item["target"]:
            return f"ERROR: item {i} needs at least 'component' and 'target'."
        item["in_scope"] = bool(raw.get("in_scope", True))
        risk = str(raw.get("risk", "medium")).lower()
        item["risk"] = risk if risk in _RISKS else "medium"
        items.append(item)
    plan = {"goal": str(plan.get("goal", "")).strip(), "summary": str(plan.get("summary", "")).strip(), "items": items}
    ui_events.set_migration_plan(plan)
    ui_events.set_proposed_packs([i["pack"] for i in items if i["in_scope"]])
    n_in = sum(1 for i in items if i["in_scope"])
    if settings.MIGRATION_PLAN_APPROVAL:
        return (f"Plan recorded and shown to the user ({n_in} in-scope item(s), {len(items) - n_in} optional). "
                "STOP now: give the user a short readable version and wait for their approval before changing any file.")
    return f"Plan recorded and shown to the user ({n_in} in-scope item(s)). Proceed with the in-scope items."


def _plan_pack_ids(pack_ids):
    """Explicit comma-separated ids win; else the approved (or proposed) plan's packs."""
    explicit = [p.strip() for p in (pack_ids or "").split(",") if p.strip()]
    if explicit:
        return explicit, "pack ids passed in"
    return ui_events.plan_packs()


def _module_of(path):
    head, sep, _ = path.partition("/src/")
    return head if sep else (os.path.dirname(path) or ".")


@tool
def list_migration_files(repo_dir: str = "repo", pack_ids: str = "") -> str:
    """List every file the plan's packs apply to: what must change, in which order, once per file.

    Built from each pack's applies_to selectors (no guessing). A file selected
    by several packs appears once with its packs in dependency order: edit it
    ONCE, applying each listed pack's guidance in that order. MUST CHANGE files
    still contain what a pack removes; VERIFY ONLY files are selected but have
    nothing known to be wrong - leave them unless the guidance names them.

    Args:
        repo_dir: The cloned repo, relative to the working directory (e.g. "repo").
        pack_ids: Optional comma-separated pack ids. Empty = the approved plan's packs
            (or the proposed plan's in-scope packs before approval).
    """
    try:
        root = workdir.resolve(repo_dir)
    except ValueError as e:
        return f"ERROR: {e}"
    if not os.path.isdir(root):
        return f"ERROR: {repo_dir} is not a directory."
    ids, source = _plan_pack_ids(pack_ids)
    if not ids:
        return "ERROR: no packs given and no plan recorded. Call propose_migration_plan first, or pass pack_ids."
    packs, unknown = techstack.resolve_packs(ids)
    if not packs:
        return f"ERROR: none of these are guideline packs: {', '.join(unknown)}."
    repo = techstack.Repo(root)
    inventory, notes = techstack.build_inventory(repo, packs)
    order = [p["id"] for p in packs]
    must = {f: e for f, e in inventory.items() if any(x["must_change"] and not x["detect_only"] for x in e)}
    manual = {f: e for f, e in inventory.items() if f not in must and any(x["must_change"] and x["detect_only"] for x in e)}
    verify = {f: e for f, e in inventory.items() if f not in must and f not in manual}

    lines = [f"Migration inventory from the {source}: {len(packs)} pack(s) in dependency order: {', '.join(order)}"]
    if unknown:
        lines.append(f"not guideline packs (ignored): {', '.join(unknown)}")
    lines.append(f"\nMUST CHANGE: {len(must)} file(s). Edit each once, applying its packs left to right.")
    by_module = {}
    for f in sorted(must):
        by_module.setdefault(_module_of(f), []).append(f)
    shown = 0
    for module, files in by_module.items():
        lines.append(f"  [{module}]")
        for f in files:
            if shown >= 300:
                break
            todo = [x["pack"] for x in must[f] if x["must_change"] and not x["detect_only"]]
            rest = [x["pack"] for x in must[f] if x["pack"] not in todo]
            lines.append(f"    {f[len(module) + 1:] if module != '.' and f.startswith(module + '/') else f}  <- {', '.join(todo)}"
                         + (f"  (verify: {', '.join(rest)})" if rest else ""))
            shown += 1
    if len(must) > shown:
        lines.append(f"    ... {len(must) - shown} more; call again with fewer pack_ids")
    if manual:
        lines.append(f"\nMANUAL (detect-only packs, no transform guidance): {len(manual)} file(s)")
        lines += [f"    {f}  <- {', '.join(x['pack'] for x in manual[f])}" for f in sorted(manual)[:40]]
    counts = {}
    for f in verify:
        counts[_module_of(f)] = counts.get(_module_of(f), 0) + 1
    lines.append(f"\nVERIFY ONLY: {len(verify)} file(s) selected with nothing known to be wrong: "
                 + "; ".join(f"{m} {n}" for m, n in sorted(counts.items())))
    if notes:
        lines.append("\nNOT EVALUATED: " + "; ".join(notes))
    lines.append("\nNEXT: migrate the MUST CHANGE files, then run check_pack_acceptance.")
    return "\n".join(lines)


@tool
def check_pack_acceptance(repo_dir: str = "repo", pack_ids: str = "") -> str:
    """Check that each pack's migration is complete, per its acceptance rules.

    Reports every leftover as file:line (a no_match pattern still present), the
    count_unchanged checks against the source branch, and the checks code cannot
    evaluate (build, parity, conditional rules) for the PR. Run it after
    migrating and again before review; a pack is done when it is clean or each
    leftover is explained in the PR.

    Args:
        repo_dir: The cloned repo, relative to the working directory (e.g. "repo").
        pack_ids: Optional comma-separated pack ids. Empty = the approved plan's packs.
    """
    try:
        root = workdir.resolve(repo_dir)
    except ValueError as e:
        return f"ERROR: {e}"
    if not os.path.isdir(root):
        return f"ERROR: {repo_dir} is not a directory."
    ids, source = _plan_pack_ids(pack_ids)
    if not ids:
        return "ERROR: no packs given and no plan recorded. Call propose_migration_plan first, or pass pack_ids."
    packs, unknown = techstack.resolve_packs(ids)
    repo = techstack.Repo(root)
    lines, open_packs = [f"Acceptance for the {source}:"], []
    for pack in packs:
        result = techstack.check_acceptance(repo, pack)
        tag = " (detect-only: manual migration)" if pack.get("status") == "detect-only" else ""
        if result["clean"]:
            lines.append(f"\n{pack['id']}{tag}: CLEAN")
        else:
            open_packs.append(pack["id"])
            lines.append(f"\n{pack['id']}{tag}: {result['leftover_count']} leftover(s)")
            lines += [f"    {l}" for l in result["leftovers"]]
            if result["leftover_count"] > len(result["leftovers"]):
                lines.append(f"    ... {result['leftover_count'] - len(result['leftovers'])} more")
        lines += [f"  {c}" for c in result["count_changes"]]
        lines += [f"  manual: {m}" for m in result["manual_checks"]]
    if unknown:
        lines.append(f"\nnot guideline packs (ignored): {', '.join(unknown)}")
    if open_packs:
        lines.append(f"\nACTION: not finished: {', '.join(open_packs)}. Migrate the leftovers (each file once), "
                     "or list each one you leave, with the reason, under 'Detected but not migrated' in the PR.")
    else:
        lines.append("\nAll packs clean. List the manual checks in the PR for the human reviewer.")
    return "\n".join(lines)


def _source_ref():
    return f"origin/{settings.GIT_SOURCE_BRANCH}" if settings.GIT_SOURCE_BRANCH else "origin/HEAD"


def _parity_findings(root):
    approved = ui_events.approved_files()
    return [f for f in techstack.test_parity(root) if f["path"] not in approved]


@tool
def check_test_parity(repo_dir: str = "repo") -> str:
    """Check that migrated tests still test the same things as on the source branch.

    A migration may change how a test is written (JUnit 4 -> 5, Mockito API), never
    what it checks. Per changed test file it compares with the source branch: test
    methods removed or renamed, fewer assertions, expected values changed. Files that
    fail go to the user for manual review; restore their checks or ask the user.

    Args:
        repo_dir: The cloned repo, relative to the working directory (e.g. "repo").
    """
    try:
        root = workdir.resolve(repo_dir)
    except ValueError as e:
        return f"ERROR: {e}"
    findings = _parity_findings(root)
    if not findings:
        return "Test parity OK: every changed test keeps its test methods, assertions and expected values."
    for f in findings:
        ok, diff = git_utils._run_git(["diff", "--no-color", _source_ref(), "--", f["path"]], root)
        guardrails.enqueue_manual_review({
            "path": f["path"], "score": 0, "threshold": settings.REVIEW_SCORE_THRESHOLD,
            "summary": "Test parity: this test no longer checks the same things as before the migration.",
            "issues": [{"severity": "high", "description": f["summary"], "location": "test methods / assertions"}],
            "diff": (diff if ok else "")[: settings.REVIEW_MAX_DIFF_CHARS], "error": "",
        })
    lines = [f"{len(findings)} test file(s) changed what they check (sent to the user for manual review):"]
    lines += [f"  - {f['path']}: {f['summary']}" for f in findings]
    lines.append("ACTION: restore the original test methods, assertions and expected values; only the JUnit/Mockito "
                 "syntax may change. Do not open the PR until these are fixed or the user approves them.")
    return "\n".join(lines)


_ENV_REF = re.compile(r'(?:System\.getenv|requiredEnv)\(\s*"([A-Za-z_][A-Za-z0-9_]*)"|\$\{(?:env\.)?([A-Z][A-Z0-9_]{2,})\}')


def _configuration_required(root):
    """Environment variables referenced in lines this branch ADDED (sorted, unique)."""
    ok, diff = git_utils._run_git(["diff", "-U0", "--no-color", f"{_source_ref()}...HEAD"], root)
    names = set()
    for line in (diff if ok else "").splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            for a, b in _ENV_REF.findall(line):
                names.add(a or b)
    return sorted(names)


def _pr_gate(root, branch_name, description):
    """(failures, facts). Failures are reasons the PR must not open yet; facts feed the PR body."""
    failures, facts = [], {"tests": [], "packs": [], "parity": [], "config": [], "root": root}
    root_real = os.path.realpath(root)
    _, dirty = git_utils._run_git(["status", "--porcelain"], root)
    if dirty.strip():
        failures.append("uncommitted changes in the working copy: commit and push them first (git_commit)")
    ok_head, head = git_utils._run_git(["rev-parse", "HEAD"], root)
    ok_remote, remote = git_utils._run_git(["rev-parse", f"origin/{branch_name}"], root)
    if not ok_remote or head.strip() != remote.strip():
        failures.append(f"HEAD is not pushed to origin/{branch_name}: push it first (create_branch / git_commit)")

    _, state = _repo_state(root)
    dirs = [d for d in _TEST_BASELINE if os.path.realpath(d).startswith(root_real)]
    dirs += [d for d in _LAST_TEST if d not in dirs and os.path.realpath(d).startswith(root_real)]
    if not dirs:
        failures.append("no test run recorded: run run_maven_test on every reactor first")
    for d in sorted(dirs):
        rel = os.path.relpath(d, root_real).replace(os.sep, "/")
        base = _TEST_BASELINE.get(d)
        last = _LAST_TEST.get(d)
        row = {"dir": rel, "baseline": base, "final": last}
        facts["tests"].append(row)
        if last is None:
            failures.append(f"{rel}: no test run after the migration - run run_maven_test on it")
        elif last["state"] != state:
            failures.append(f"{rel}: the last test run is older than the current code - run run_maven_test again")
        elif not last["compiled"]:
            failures.append(f"{rel}: does not compile ({last['compile_errors']} compile/pom error(s)) - fix it")
        elif last["new_failures"]:
            failures.append(f"{rel}: {len(last['new_failures'])} new test failure(s) vs the baseline: "
                            + ", ".join(last["new_failures"][:8]))

    ids, source = ui_events.plan_packs()
    packs, _ = techstack.resolve_packs(ids)
    repo = techstack.Repo(root)
    waived = ui_events.waived_packs()
    facts["unfinished"] = []
    for pack in packs:
        result = techstack.check_acceptance(repo, pack)
        facts["packs"].append((pack, result))
        # Approved means migrated: an unfinished approved pack blocks the PR. Naming it in
        # the description does not count - only the user can accept leftovers (waive).
        if result["clean"] or pack.get("status") == "detect-only" or pack["id"] in waived:
            continue
        facts["unfinished"].append((pack["id"], result["leftover_count"]))
        failures.append(f"pack {pack['id']} is approved but unfinished: {result['leftover_count']} leftover(s) - "
                        "migrate them (next_migration_files). You may not descope it; only the user can accept leftovers.")

    parity = _parity_findings(root)
    facts["parity"] = parity
    for f in parity:
        failures.append(f"test parity: {f['path']} - {f['summary'][:160]} (restore the checks, or the user approves the file)")
    facts["config"] = _configuration_required(root)
    return failures, facts


def _fmt_counts(entry, key):
    if not entry:
        return "-"
    if key == "baseline":
        return (f"{len(entry['failing'])} failing" if entry["reached_tests"] else "build did not reach the tests")
    if not entry["compiled"]:
        return f"DOES NOT COMPILE ({entry['compile_errors']} error(s))"
    c = entry["counts"] or {}
    return f"{c.get('run', 0)} run, {c.get('failures', 0) + c.get('errors', 0)} failing, {len(entry['new_failures'])} new"


def _verification_section(facts, failures):
    lines = ["", "---", "## Verification (generated by forge from tool results)", ""]
    if failures:
        lines += ["> **PR gate failed - this is a draft.** Open items:", ""] + [f"> - {f}" for f in failures] + [""]
    lines += ["### Tests", "", "| Reactor | Before (baseline) | After |", "| --- | --- | --- |"]
    for row in facts["tests"]:
        lines.append(f"| `{row['dir']}` | {_fmt_counts(row['baseline'], 'baseline')} | {_fmt_counts(row['final'], 'final')} |")
    lines += ["", "### Guideline packs", "", "| Pack | Status |", "| --- | --- |"]
    for pack, result in facts["packs"]:
        status = "CLEAN" if result["clean"] else f"{result['leftover_count']} leftover(s): " + "; ".join(
            l.replace("|", "\\|") for l in result["leftovers"][:5])
        lines.append(f"| `{pack['id']}` | {status} |")
    manual = sorted({m for _, r in facts["packs"] for m in r["manual_checks"]})
    if manual:
        lines += ["", "Manual checks for the reviewer:"] + [f"- {m}" for m in manual]
    if facts["parity"]:
        lines += ["", "### Test parity", ""] + [f"- `{f['path']}`: {f['summary']}" for f in facts["parity"]]
    waived = sorted(ui_events.waived_packs())
    if waived:
        lines += ["", "Not migrated (accepted by the user): " + ", ".join(f"`{w}`" for w in waived)]
    approved = sorted(ui_events.approved_files())
    if approved:
        lines += ["", "Approved as-is by the user after manual review: " + ", ".join(f"`{a}`" for a in approved)]
    scan = pii_scan.LAST_SCAN.get(os.path.realpath(facts.get("root", "")))
    if scan and scan["findings"]:
        found = scan["findings"]
        lines += ["", "### Personal data (Amazon Bedrock scan)", "",
                  f"{len(found)} item(s) in {len({f.path for f in found})} file(s): {pii_scan.summary_by_type(found)}. "
                  "Values are not shown; check whether these are real people's data and replace them with synthetic data.", ""]
        lines += [f"- `{f.path}:{f.line}` {f.kind.split(': ', 1)[-1]}" for f in found[:50]]
        if len(found) > 50:
            lines.append(f"- ... {len(found) - 50} more")
        if scan["truncated"]:
            lines.append(f"\nThe scan stopped at its budget of {settings.PII_SCAN_MAX_CHARS} characters.")
    if facts["config"]:
        lines += ["", "### Configuration required", "", "Environment variables the migrated code reads:", ""]
        lines += [f"- `{name}`" for name in facts["config"]]
    return "\n".join(lines)


@tool
def create_pull_request(github_url: str, branch_name: str, pr_title: str, pr_description: str) -> str:
    """Open the pull request - only after the PR gate passes (authenticates automatically).

    The gate checks, from tool results: the branch is committed and pushed; every
    reactor's latest run_maven_test ran on the current code, compiled, and has no
    new failures vs the baseline; every approved pack is clean or named under
    'Detected but not migrated' in the description; tests keep their meaning
    (check_test_parity) unless the user approved the file. If it refuses, fix the
    listed items - do not argue or relabel failures as pre-existing. A verification
    section generated from tool results is appended to the description; do not
    restate test results or versions in your own words.

    Args:
        github_url: The repo URL that was cloned.
        branch_name: The pushed branch the PR is opened from.
        pr_title: Pull request title.
        pr_description: Pull request body (markdown).
    """
    try:
        root = workdir.resolve("repo")
    except ValueError as e:
        return f"ERROR: {e}"
    mode = settings.PR_GATE
    failures, facts = ([], None) if mode == "off" else _pr_gate(root, branch_name, pr_description)
    if failures and mode == "block":
        logger.info(f"PR gate refused ({len(failures)} item(s))")
        if facts and facts.get("unfinished"):
            ui_events.push_gate_refusal(facts["unfinished"], failures)
        return ("ERROR: PR gate refused - the pull request was NOT opened. Fix these, then call create_pull_request again:\n"
                + "\n".join(f"  - {f}" for f in failures))
    body = pr_description + (_verification_section(facts, failures) if facts else "")
    return git_utils.open_pull_request(github_url, branch_name, pr_title, body, draft=bool(failures))


_QUEUE_START = {}   # (root, packs) -> the MUST CHANGE set at the first call, for progress


def _queue_rank(path):
    """Main code first (tests compile against it), then tests, then everything else."""
    if "/src/main/java/" in path or path.startswith("src/main/java/"):
        return 0
    if "/src/test/" in path or path.startswith("src/test/"):
        return 2
    return 1  # resources, webapp (JSPs, web.xml), poms, Docker/config


@tool
def next_migration_files(repo_dir: str = "repo", limit: int = 8) -> str:
    """The next files to migrate for the approved plan, in the order to do them, with progress.

    Use this as the Phase 4 worklist: migrate the files it returns (each once,
    applying the listed packs left to right), then call it again until it says no
    files are left. Order is fixed by code: main sources first, then
    resources/webapp/config, then tests; within each, packs in dependency order. Do not skip a file and do
    not declare files or packs out of scope - only the user can.

    Args:
        repo_dir: The cloned repo, relative to the working directory (e.g. "repo").
        limit: How many files to return (1-20).
    """
    try:
        root = workdir.resolve(repo_dir)
    except ValueError as e:
        return f"ERROR: {e}"
    ids, source = ui_events.plan_packs()
    if not ids:
        return "ERROR: no plan recorded. Call propose_migration_plan and wait for approval first."
    packs, _ = techstack.resolve_packs(ids)
    packs = [p for p in packs if p.get("status") != "detect-only" and p["id"] not in ui_events.waived_packs()]
    order = {p["id"]: i for i, p in enumerate(packs)}
    inventory, _ = techstack.build_inventory(techstack.Repo(root), packs)
    todo = {}
    for path, entries in inventory.items():
        work = [e["pack"] for e in entries if e["must_change"] and not e["detect_only"]]
        if work:
            todo[path] = work
    key = (root, tuple(p["id"] for p in packs))
    start = _QUEUE_START.setdefault(key, set(todo))
    start |= set(todo)  # files can become MUST CHANGE later (e.g. a new leftover); count them too
    done = len(start - set(todo))
    if not todo:
        return (f"Progress: {done} of {len(start)} files done. No MUST CHANGE files left for the {source}: "
                "run check_pack_acceptance and check_test_parity, then the tests.")
    # Main code first across all packs (tests compile against it), then pack order, then path.
    ranked = sorted(todo, key=lambda f: (_queue_rank(f), min(order.get(p, 99) for p in todo[f]), f))
    limit = max(1, min(int(limit or 8), 20))
    lines = [f"Progress: {done} of {len(start)} files done ({len(todo)} left) for the {source}.",
             f"Next {min(limit, len(ranked))} file(s) - migrate each once, applying its packs left to right:"]
    lines += [f"  {f}  <- {', '.join(sorted(todo[f], key=lambda p: order.get(p, 99)))}" for f in ranked[:limit]]
    lines.append("Then call next_migration_files again. Do not skip a file; only the user can take a file or pack "
                 "out of scope. Query code_upgrade_knowledge_base for a pack's guidance if you have not yet.")
    return "\n".join(lines)


@tool
def get_current_timestamp():
    """ Get the current date and time """
    return datetime.now()    
