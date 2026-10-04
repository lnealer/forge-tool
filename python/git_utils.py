import os
from datetime import datetime
from abc import ABC, abstractmethod

import requests
from black import FileMode, format_str
from git import Repo
from langchain_core.tools import tool
import subprocess
import re
import stat

import guardrails
import settings
import ui_events
import workdir
from utils import get_logger

logger = get_logger()

def remove_readonly(func, path, excinfo):
    os.chmod(path, stat.S_IWRITE)
    func(path)

_GITHUB_TOKEN = ""


def configure_github_token(token):
    """Hold the PAT in process memory.

    The tools read it from here instead of the model passing it as an
    argument, so the token never enters the prompt, the conversation history
    or the Bedrock request body. That is also what lets the Bedrock guardrail
    block token patterns without blocking every request.
    """
    global _GITHUB_TOKEN
    _GITHUB_TOKEN = token or ""


def _github_token():
    if not _GITHUB_TOKEN:
        raise RuntimeError(
            "GitHub token not configured; configure_github_token() must run at startup."
        )
    return _GITHUB_TOKEN


def _authed_https_url(url):
    """Build an HTTPS clone URL that authenticates with the GitHub PAT.

    Clone and push both use origin, so embedding the token here means later
    pushes need no extra auth plumbing. The token is written into the clone's
    .git/config; that is the same temp dir the old SSH key lived in.
    """
    return f"https://x-access-token:{_github_token()}@github.com/{_repo_slug(url)}.git"


def _sanitize(text):
    """Redact an embedded token before logging a URL or git output."""
    return re.sub(r"(https://[^:@/]+:)[^@/]+(@)", r"\1***\2", text)


def _run_git(args, cwd):
    """Run a git command and return (ok, combined_output).

    No shell: a list argv with shell=True runs bare `git` on POSIX and silently
    drops every argument. cwd is passed per call rather than using os.chdir,
    which would mutate the whole Streamlit process. The token rides on origin's
    URL, so no auth env is needed here.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"

    logger.info(f"Running: git {' '.join(args)} (cwd={cwd})")
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8", errors="replace",  # Windows would decode with the ANSI code page otherwise
        check=False,
    )
    output = (result.stdout or "") + (result.stderr or "")
    output = _sanitize(output.strip())
    if result.returncode != 0:
        logger.info(f"git {args[0]} failed ({result.returncode}): {output}")
    return result.returncode == 0, output


@tool
def clone_repo(url, repo_dir, branch=""):
    """Clone the target repo into the working directory (authenticates automatically).

    Args:
        url: Repo URL, e.g. https://github.com/org/repo.git (ssh form also accepted).
        repo_dir: Directory name to clone into, relative to the working directory (e.g. "repo").
        branch: Branch to check out. Leave empty for the repo's default branch.
    """
    # Relative names are anchored to the sandbox; an unanchored name would be
    # resolved by git against the server process's own working directory.
    try:
        target = workdir.resolve(repo_dir.replace("\\", "/"))
    except ValueError as e:
        return f"ERROR: {e}"
    rel = os.path.relpath(target, workdir.get_working_dir())
    usage = (
        f"Use '{rel}/...' (relative to tmpdir) with read_file, write_file, list_directory, "
        "run_maven_test, run_maven_install, scan_for_secrets, create_branch and git_commit."
    )

    # Idempotent: a repeated call for the same repo returns the existing clone
    # instead of failing on "destination path already exists".
    if os.path.isdir(os.path.join(target, ".git")):
        try:
            existing = Repo(target)
            origin = existing.remotes.origin.url if existing.remotes else ""
            # Same repo if origin is exactly what we would clone from (same token)
            # or, after a token rotation, the same GitHub owner/repo.
            same = origin == _authed_https_url(url) or (
                "github.com" in origin
                and origin.split("github.com")[-1].lstrip(":/").removesuffix(".git") == _repo_slug(url)
            )
            if same:
                head = existing.active_branch.name
                return f"Already cloned at {target} (branch '{head}'). {usage}"
        except Exception:
            pass
        return f"ERROR: {target} already exists and is a different repository; choose another repo_dir."

    try:
        logger.info(f"Cloning repo {url} to {target} (branch={branch or '<default>'})")
        clone_kwargs = {}
        if branch:
            clone_kwargs["branch"] = branch

        if os.name == "nt":
            # Python writes text files with CRLF on Windows: autocrlf keeps those from
            # showing as whole-file diffs, and longpaths lifts the 260-char limit for
            # deep Maven trees. Fixed options, never model input.
            clone_kwargs["multi_options"] = ["--config core.autocrlf=true", "--config core.longpaths=true"]
            clone_kwargs["allow_unsafe_options"] = True
        repo = Repo.clone_from(_authed_https_url(url), target, **clone_kwargs)
        with repo.config_writer() as config:
            config.set_value("user", "name", settings.GIT_COMMIT_AUTHOR_NAME)
            config.set_value("user", "email", settings.GIT_COMMIT_AUTHOR_EMAIL)

        head = repo.active_branch.name
        logger.info(f"Cloned {url} into {target} at branch {head}")
        # Return text, not the Repo object: the agent only sees str(result).
        return f"Cloned {url} into {target}. Checked out branch '{head}'. {usage}"
    except Exception as e:
        logger.info(f"Failed cloning {url}: {_sanitize(str(e))}")
        return f"ERROR: failed to clone {url}: {_sanitize(str(e))}"

def format(content):
    """Format code."""
    return format_str(content, mode=FileMode())


def _stage_and_scan(repo_file_path):
    """Stage everything, then refuse to continue if the staged diff adds a secret.

    Returns an error string for the agent, or None when the stage is clean.
    Only added lines are scanned, so a key already in the repo's history is
    reported by scan_for_secrets rather than blocking every commit.
    """
    ok, output = _run_git(["add", "-A"], repo_file_path)
    if not ok:
        return f"ERROR: git add failed: {output}"
    if not settings.SECRET_SCAN_ENABLED:
        return None

    ok, diff = _run_git(["diff", "--cached", "-U0", "--no-color"], repo_file_path)
    if not ok:
        return f"ERROR: could not read the staged diff: {diff}"
    findings = guardrails.scan_diff(diff)
    if not findings:
        return None

    _run_git(["reset", "-q"], repo_file_path)
    ui_events.push_secret_block("commit blocked", repo_file_path, findings)
    logger.warning(
        f"Secret guardrail blocked a commit in {repo_file_path}:\n"
        f"{guardrails.format_findings(findings)}"
    )
    return (
        "ERROR: commit blocked by the secret guardrail. The staged changes add what "
        "looks like secret material. Remove these values from the source (read them "
        "from environment variables or a secrets manager instead) and try again:\n"
        + guardrails.format_findings(findings)
    )


def _current_branch(repo_file_path):
    ok, out = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_file_path)
    return out.strip() if ok else ""


def _branch_exists(repo_file_path, name):
    ok, _ = _run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], repo_file_path)
    return ok


@tool
def create_branch(repo_file_path, commit_message, branch_name=""):
    """Commit all changes on an upgrade branch and push it to the remote.

    Leave branch_name empty: the tool names the branch forge-upgrade-<timestamp>
    itself, or keeps using the current forge-upgrade-* branch when one is already
    checked out, so repeated calls never collide. The result states the branch
    name to use for create_pull_request.

    Args:
        repo_file_path: Path of the cloned repo (relative to the working directory or absolute).
        commit_message: Commit message describing the upgrade.
        branch_name: Optional explicit branch name; usually leave empty.
    """
    try:
        repo_file_path = workdir.resolve(repo_file_path.replace("\\", "/"))
    except ValueError as e:
        return f"ERROR: {e}"
    if not os.path.isdir(repo_file_path):
        return f"ERROR: {repo_file_path} is not a directory. Clone the repo first."

    # Stage and scan before touching branches, so a blocked commit leaves no
    # half-created branch behind.
    blocked = _stage_and_scan(repo_file_path)
    if blocked:
        return blocked

    current = _current_branch(repo_file_path)
    target = (branch_name or "").strip()
    if not target:
        if current.startswith("forge-upgrade-"):
            target = current  # continue the branch this run already started
        else:
            target = "forge-upgrade-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    if target != current:
        args = ["checkout", target] if _branch_exists(repo_file_path, target) else ["checkout", "-b", target]
        ok, output = _run_git(args, repo_file_path)
        if not ok:
            return f"ERROR: git checkout failed: {output}"

    ok, output = _run_git(["commit", "-m", commit_message], repo_file_path)
    if not ok:
        if "nothing to commit" in output:
            return f"ERROR: nothing to commit on '{target}' - no file changes were written."
        return f"ERROR: git commit failed: {output}"

    ok, output = _run_git(["push", "-u", "origin", target], repo_file_path)
    if not ok:
        return f"ERROR: git push failed: {output}"
    return f"Pushed branch '{target}' to origin. Use branch_name='{target}' for create_pull_request."


@tool
def git_commit(repo_file_path, commit_message, branch_name=""):
    """Commit follow-up changes and push them to the current upgrade branch.

    Args:
        repo_file_path: Path of the cloned repo (relative to the working directory or absolute).
        commit_message: Commit message describing the follow-up change.
        branch_name: Optional; defaults to the branch currently checked out.
    """
    try:
        repo_file_path = workdir.resolve(repo_file_path.replace("\\", "/"))
    except ValueError as e:
        return f"ERROR: {e}"
    if not os.path.isdir(repo_file_path):
        return f"ERROR: {repo_file_path} is not a directory."

    blocked = _stage_and_scan(repo_file_path)
    if blocked:
        return blocked

    target = (branch_name or "").strip() or _current_branch(repo_file_path)
    current = _current_branch(repo_file_path)
    if target and target != current:
        ok, output = _run_git(["checkout", target], repo_file_path)
        if not ok:
            return f"ERROR: git checkout {target} failed: {output}"

    ok, output = _run_git(["commit", "-m", commit_message], repo_file_path)
    if not ok:
        if "nothing to commit" in output:
            return "No changes to commit; nothing was pushed."
        return f"ERROR: git commit failed: {output}"

    ok, output = _run_git(["push", "-u", "origin", target], repo_file_path)
    if not ok:
        return f"ERROR: git push failed: {output}"
    return f"Pushed follow-up commit to '{target}'."


@tool
def git_status(repo_file_path):
    """Show the current branch, changed files and recent commits of the clone.

    Use it to pick up where a previous turn stopped, or before create_branch /
    create_pull_request to confirm which branch the work is on.

    Args:
        repo_file_path: Path of the cloned repo (relative to the working directory or absolute).
    """
    try:
        repo_file_path = workdir.resolve(repo_file_path.replace("\\", "/"))
    except ValueError as e:
        return f"ERROR: {e}"
    if not os.path.isdir(os.path.join(repo_file_path, ".git")):
        return f"ERROR: {repo_file_path} is not a git repository."
    branch = _current_branch(repo_file_path)
    _, status = _run_git(["status", "--short"], repo_file_path)
    _, log = _run_git(["log", "--oneline", "-5"], repo_file_path)
    _, upstream = _run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], repo_file_path)
    changed = status.splitlines()
    shown = "\n".join(changed[:60]) + (f"\n... {len(changed) - 60} more" if len(changed) > 60 else "")
    return (f"branch: {branch}" + (f" (tracks {upstream.strip()})" if upstream and not upstream.startswith("fatal") else " (not pushed yet)")
            + f"\nuncommitted changes: {len(changed)}\n{shown or '(clean)'}\nrecent commits:\n{log}")


@tool
def git_restore_file(repo_file_path, file_path):
    """Discard the upgrade's changes to one file by restoring the source-branch version.

    Use this when the user rejects a reviewed file. The file is reset to how it
    is on origin/<source branch>, and will not be part of the pull request.

    Args:
        repo_file_path: Path of the cloned repo (relative to the working directory or absolute).
        file_path: Path of the file inside the repo, e.g. ams-common/pom.xml.
    """
    try:
        repo_file_path = workdir.resolve(repo_file_path.replace("\\", "/"))
    except ValueError as e:
        return f"ERROR: {e}"
    ref = f"origin/{settings.GIT_SOURCE_BRANCH}" if settings.GIT_SOURCE_BRANCH else "origin/HEAD"
    ok, output = _run_git(["checkout", ref, "--", file_path], repo_file_path)
    if not ok:
        return f"ERROR: could not restore {file_path} from {ref}: {output}"
    return f"Restored {file_path} to its {ref} version; it is no longer part of this upgrade."


def open_pull_request(github_url, branch_name, pr_title, pr_description, draft=False):
    """POST the pull request to GitHub. The agent's tool (agent.create_pull_request)
    runs the PR gate first; this is only the transport."""
    repo_api_url = get_github_api_url(github_url)
    git_provider = GitHubProvider(_github_token(), repo_api_url)
    logger.info(f"Creating pull request. repo_api_url={repo_api_url}, branch_name={branch_name}, draft={draft}")
    return git_provider.create_pull_request(branch_name, pr_title, pr_description, draft=draft)


def _repo_slug(github_url):
    """Extract 'owner/repo' from an https or ssh GitHub URL.

    Raises ValueError on a URL that is neither form, instead of the opaque
    AttributeError the old re.search(...).group(1) produced.
    """
    for pattern in (r"git@github\.com:(.*?)(?:\.git)?$",
                    r"https://github\.com/(.*?)(?:\.git)?$"):
        match = re.search(pattern, github_url.strip())
        if match:
            return match.group(1)
    raise ValueError(
        f"Unrecognized GitHub URL: {github_url!r}. "
        "Expected https://github.com/owner/repo(.git) or git@github.com:owner/repo(.git)."
    )


def get_github_api_url(github_url):
    """Get the GitHub REST API URL for a repo."""
    return f"https://api.github.com/repos/{_repo_slug(github_url)}"

# can be implemented for other git platforms (e.g. gitlab)
class GitProvider(ABC):
    @abstractmethod
    def create_pull_request(branch_name):
        pass


class GitHubProvider(GitProvider):
    """GitHub provider.

    Interacts with the GitHub API to perform git operations.
    """

    def __init__(self, api_key, repo_url):
        self.url = f"{repo_url}/pulls"
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": settings.GITHUB_API_VERSION,
        }

    def create_pull_request(self, branch, title, description, draft=False):
        """Create a new pull request for a target branch"""
        data = {
            "title": title,
            "body": description,
            "head": branch,
            "base": settings.GIT_BASE_BRANCH,
            "draft": bool(draft),
        }

        url = self.url
        logger.info(url)
        response = requests.post(
            url, json=data, headers=self.headers, timeout=settings.GITHUB_API_TIMEOUT
        )

        if response.status_code == 201:
            logger.info(f"Pull request created ({response.json()['html_url']})")
            return response.text or "Pull request created"
        else:
            logger.info(f"Failed to create pull request with error: {response.text}")
            return response.text