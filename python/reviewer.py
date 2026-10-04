"""Second-opinion reviewer: Amazon Nova Pro scores each file the primary model migrated.

The primary (Claude) agent calls ``review_migrated_files`` after upgrading.
Each changed file's diff is sent to Nova Pro with a rubric; it returns a 0-10
score and a list of issues. Files scoring below ``REVIEW_SCORE_THRESHOLD`` are
pushed onto the manual-review queue, which chat.py renders so a human decides
before the PR goes out.
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import List

from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

import guardrails
import settings
import techstack
import ui_events
import workdir
from git_utils import _run_git
from utils import get_guardrail_config, get_logger

logger = get_logger()

REVIEW_SYSTEM_PROMPT = """You are a senior Java reviewer auditing an automated code migration.
You will be given the upgrade goal, the WHOLE file after the migration, and the unified diff
another AI produced for it. Score how correct, complete and safe the migration of this file is.

Judge by evidence in the file, not by what might exist elsewhere:
- A change is complete when no other line of the file still uses the old API. An import-only
  change (javax -> jakarta) is complete if nothing else in the file references the old API.
- Every issue must name a line or symbol that is actually in the file or the diff. Do not report
  speculative issues ("might be using", "could need", "ensure that", "entire file").
- With no concrete issue, the score is 8-10.
- When pack guidance is given, it is the authority for this migration. Never raise an issue that
  contradicts it or that would not compile: for example, JDK packages such as javax.xml.parsers,
  javax.sql and javax.crypto stay javax.* (there is no jakarta.sql); JUnit 5 assertions take the
  message as the LAST argument; Jakarta Tags 3.0 URIs are jakarta.tags.core / jakarta.tags.fmt.
  Unsure whether an API exists? Do not raise the issue.

Scoring rubric (0-10):
 10  Correct, complete, idiomatic for the target version; nothing to change.
 8-9 Correct; minor style or polish nits only.
 6-7 Mostly right but has a gap a reviewer must check (missed API change, untested path).
 4-5 Likely broken or incomplete: wrong API usage, lost behaviour, unresolved compile risk.
 0-3 Unsafe or clearly wrong: deleted logic, security regression, secrets, unrelated edits.

Also fail (score <= 3) if the diff introduces credentials, keys, or edits unrelated to the upgrade.

Respond with ONLY a JSON object, no prose, in exactly this shape:
{"score": <integer 0-10>, "summary": "<one or two sentences>",
 "issues": [{"severity": "high|medium|low", "description": "<what and why>", "location": "<line or symbol>"}]}
"""


@dataclass
class ReviewResult:
    path: str
    score: int
    summary: str
    issues: List[dict] = field(default_factory=list)
    raw: str = ""
    error: str = ""

    @property
    def flagged(self):
        return self.score < settings.REVIEW_SCORE_THRESHOLD

    def as_markdown(self):
        status = "NEEDS MANUAL REVIEW" if self.flagged else "ok"
        lines = [f"**{self.path}** — score {self.score}/10 ({status})", f"{self.summary}"]
        for issue in self.issues:
            lines.append(
                f"- [{issue.get('severity', '?')}] {issue.get('description', '')}"
                + (f" ({issue['location']})" if issue.get("location") else "")
            )
        if self.error:
            lines.append(f"- reviewer error: {self.error}")
        return "\n".join(lines)


_LANG = {".java": "java", ".xml": "xml", ".jsp": "jsp", ".properties": "properties",
         ".yml": "yaml", ".yaml": "yaml", ".sql": "sql", ".js": "javascript", ".html": "html"}


def build_review_message(path, diff, upgrade_details, content, guidance=None):
    """Goal, the whole file after the migration, then the diff, within REVIEW_MAX_DIFF_CHARS.

    The diff is kept whole first (it is what changed); the file takes the rest
    of the budget and is truncated with a marker when it does not fit.
    """
    budget = settings.REVIEW_MAX_DIFF_CHARS
    if len(diff) > budget:
        diff = diff[:budget] + "\n... [diff truncated for review]"
    room = max(budget - len(diff), 4000)
    if len(content) > room:
        content = content[:room] + "\n... [file truncated for review]"
    lang = _LANG.get(os.path.splitext(path)[1].lower(), "")
    file_block = (f"File after the migration:\n```{lang}\n{content}\n```\n\n" if content
                  else "File after the migration: (deleted or not readable)\n\n")
    guide_block = ""
    if guidance:
        parts, used = [], 0
        for pack_id, text in guidance:
            text = text[: max(0, 12000 - used)]
            if not text:
                break
            parts.append(f"### Pack {pack_id}\n{text}")
            used += len(text)
        guide_block = ("Pack guidance for this file (the authority for this migration):\n"
                       + "\n\n".join(parts) + "\n\n")
    return (f"Upgrade goal: {upgrade_details}\n"
            f"File: {path}\n\n"
            f"{guide_block}"
            f"{file_block}"
            f"Unified diff against the source branch:\n```diff\n{diff}\n```")


def _file_after(repo_path, path):
    """The working-copy file as the migration left it ("" if deleted or unreadable)."""
    full = os.path.join(repo_path, path)
    try:
        with open(full, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _parse_review(raw):
    """Pull the JSON object out of the model reply, tolerating stray prose or fences."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in reviewer reply")
    data = json.loads(text[start : end + 1])
    score = int(data.get("score", 0))
    score = max(0, min(10, score))
    issues = data.get("issues") or []
    if not isinstance(issues, list):
        issues = []
    return score, str(data.get("summary", "")).strip(), issues


class MigrationReviewer:
    """Thin wrapper around a Nova Pro chat model with the review rubric."""

    def __init__(self):
        kwargs = {
            "model": settings.REVIEWER_MODEL_ID,
            "region_name": settings.REVIEWER_MODEL_REGION,
            "temperature": 0.0,
            "max_tokens": settings.REVIEWER_MAX_TOKENS,
        }
        guardrail = get_guardrail_config()
        if guardrail:
            kwargs["guardrail_config"] = guardrail
        logger.info(
            f"Initializing reviewer model {settings.REVIEWER_MODEL_ID} "
            f"in {settings.REVIEWER_MODEL_REGION} (guardrail={'on' if guardrail else 'off'})"
        )
        self.llm = ChatBedrockConverse(**kwargs)

    def review(self, path, diff, upgrade_details, content="", guidance=None):
        user = build_review_message(path, diff, upgrade_details, content, guidance)
        try:
            reply = self.llm.invoke([SystemMessage(REVIEW_SYSTEM_PROMPT), HumanMessage(user)])
            raw = reply.content if isinstance(reply.content, str) else str(reply.content)
            score, summary, issues = _parse_review(raw)
            return ReviewResult(path, score, summary, issues, raw=raw)
        except Exception as e:  # a failed review must not kill the run; flag it instead
            logger.warning(f"Reviewer failed on {path}: {e}")
            return ReviewResult(
                path, 0, "Reviewer could not score this file.", error=str(e)
            )


_reviewer = None


def get_reviewer():
    global _reviewer
    if _reviewer is None:
        _reviewer = MigrationReviewer()
    return _reviewer


def _diff_base():
    """Ref the migration is compared against: the source branch on origin."""
    if settings.GIT_SOURCE_BRANCH:
        return f"origin/{settings.GIT_SOURCE_BRANCH}"
    return "origin/HEAD"


def _changed_files(repo_path, base):
    ok, out = _run_git(["diff", "--name-only", base, "--"], repo_path)
    if not ok:
        return None, out
    ok2, untracked = _run_git(["ls-files", "--others", "--exclude-standard"], repo_path)
    files = [f for f in out.splitlines() if f.strip()]
    if ok2:
        files += [f for f in untracked.splitlines() if f.strip()]
    return sorted(set(files)), ""


def _file_diff(repo_path, base, path):
    ok, out = _run_git(["diff", "--no-color", base, "--", path], repo_path)
    if ok and out.strip():
        return out
    # New untracked file: show it as an all-added diff.
    ok, out = _run_git(["diff", "--no-color", "--no-index", "/dev/null", path], repo_path)
    return out if out.strip() else ""


@tool
def review_migrated_files(repo_file_path, upgrade_details, file_paths=""):
    """Have the reviewer model (Amazon Nova Pro) score the migration of each changed file.

    Call this after the upgrade is written and tests pass, before pushing.
    Files scoring below the threshold are sent to the user for manual review in
    the chat; do not open the pull request until the user has responded.

    Args:
        repo_file_path: Absolute path of the cloned repo.
        upgrade_details: The upgrade goal, e.g. "Java 21".
        file_paths: Optional comma-separated repo-relative paths. Empty = every changed file.
    """
    try:
        repo_file_path = workdir.resolve(repo_file_path)
    except ValueError as e:
        return f"ERROR: {e}"
    base = _diff_base()
    if file_paths.strip():
        files = [p.strip() for p in file_paths.split(",") if p.strip()]
    else:
        files, err = _changed_files(repo_file_path, base)
        if files is None:
            return f"ERROR: could not list changed files against {base}: {err}"
    if not files:
        return f"No changed files found against {base}; nothing to review."

    reviewer = get_reviewer()
    owners, packs_by_id = {}, {}
    try:
        ids, _ = ui_events.plan_packs()
        packs, _ = techstack.resolve_packs(ids)
        packs_by_id = {p["id"]: p for p in packs}
        owners = techstack.packs_for_files(techstack.Repo(repo_file_path), packs)
    except Exception as e:  # guidance is an aid; never fail the review over it
        logger.warning(f"Pack guidance for the reviewer unavailable: {e}")
    results = []
    for path in files:
        diff = _file_diff(repo_file_path, base, path)
        if not diff:
            continue
        guidance = [(pid, techstack.pack_guidance(packs_by_id[pid])) for pid in owners.get(path, []) if pid in packs_by_id]
        result = reviewer.review(path, diff, upgrade_details, _file_after(repo_file_path, path), guidance)
        results.append(result)
        if result.flagged:
            guardrails.enqueue_manual_review(
                {
                    "path": path,
                    "score": result.score,
                    "threshold": settings.REVIEW_SCORE_THRESHOLD,
                    "summary": result.summary,
                    "issues": result.issues,
                    "diff": diff[: settings.REVIEW_MAX_DIFF_CHARS],
                    "error": result.error,
                }
            )

    flagged = [r for r in results if r.flagged]
    report = [
        f"Reviewer: {settings.REVIEWER_MODEL_ID}, threshold {settings.REVIEW_SCORE_THRESHOLD}/10, "
        f"{len(results)} file(s) reviewed, {len(flagged)} flagged.",
        "",
    ]
    report += [r.as_markdown() for r in results]
    if flagged:
        report += [
            "",
            f"{len(flagged)} file(s) scored below the threshold and have been sent to the user "
            "for manual review in the chat. Tell the user which files need their review, "
            "summarize the reviewer's concerns, and WAIT for their decision before pushing "
            "or opening the pull request.",
        ]
    else:
        report += ["", "All files passed review. You may proceed to push and open the PR."]
    return "\n".join(report)
