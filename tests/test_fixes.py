"""Branch tool, Maven summary + baseline, step-limit pause, prompt placeholders.

Offline: no AWS calls (Bedrock and GitHub are stubbed), git runs against local
bare repositories in a temp dir. Run directly or through tests/run_all.py.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON_DIR = os.path.join(os.path.dirname(HERE), "python")
sys.path.insert(0, PYTHON_DIR)
os.chdir(PYTHON_DIR)
import os, subprocess, sys, tempfile, types
import workdir, git_utils, ui_events, settings

def sh(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()

# ---------- 1. create_branch / git_commit / git_status against a local bare remote ----------
tmp = tempfile.mkdtemp(); workdir.set_working_dir(tmp)
remote = os.path.join(tmp, "remote.git"); os.makedirs(remote)
sh(["git", "init", "-q", "--bare", "-b", "simple-app", remote], tmp)
seed = os.path.join(tmp, "seed"); os.makedirs(seed)
sh(["git", "init", "-q", "-b", "simple-app"], seed)
sh(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"], seed)
sh(["git", "remote", "add", "origin", remote], seed); sh(["git", "push", "-q", "origin", "simple-app"], seed)
sh(["git", "clone", "-q", "-b", "simple-app", remote, os.path.join(tmp, "repo")], tmp)
repo = os.path.join(tmp, "repo")
sh(["git", "config", "user.name", "t"], repo); sh(["git", "config", "user.email", "t@t"], repo)

def write(name, text):
    with open(os.path.join(repo, name), "w") as f: f.write(text)

# 1a. blocked commit leaves no branch behind
write("app.properties", 'db.pass' + 'word="Sup3rSecret!"\n')
out = git_utils.create_branch.invoke({"repo_file_path": "repo", "commit_message": "leak"})
assert "blocked" in out.lower(), out
assert sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo) == "simple-app", "branch created despite block"
assert sh(["git", "diff", "--cached", "--name-only"], repo) == "", "staged files not reset"
ev = ui_events.drain_secret_blocks(); assert ev and ev[0]["kind"] == "commit blocked", ev
print("1a ok: blocked commit -> no branch, index reset, chat event")

# 1b. placeholder fix passes; tool names the branch
write("app.properties", "db.password=${DB_PASSWORD}\n")
out = git_utils.create_branch.invoke({"repo_file_path": "repo", "commit_message": "externalize"})
assert out.startswith("Pushed branch 'forge-upgrade-"), out
branch = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo); assert branch.startswith("forge-upgrade-"), branch
assert branch in sh(["git", "branch", "-r"], repo), "not pushed"
print("1b ok:", out)

# 1c. second call with no name reuses the branch (no 'already exists')
write("b.txt", "x\n")
out2 = git_utils.create_branch.invoke({"repo_file_path": "repo", "commit_message": "more"})
assert out2 == f"Pushed branch '{branch}' to origin. Use branch_name='{branch}' for create_pull_request.", out2
assert sh(["git", "rev-list", "--count", f"origin/simple-app..{branch}"], repo) == "2"
print("1c ok: reused", branch)

# 1d. explicit name that already exists -> checkout, not failure (the pasted error)
sh(["git", "checkout", "-q", "simple-app"], repo); sh(["git", "checkout", "-q", "-b", "forge-upgrade-20250115-1200"], repo)
sh(["git", "checkout", "-q", "simple-app"], repo)
write("c.txt", "y\n")
out3 = git_utils.create_branch.invoke({"repo_file_path": "repo", "commit_message": "explicit", "branch_name": "forge-upgrade-20250115-1200"})
assert out3.startswith("Pushed branch 'forge-upgrade-20250115-1200'"), out3
print("1d ok: existing explicit branch checked out and pushed")

# 1e. git_commit without branch_name commits on the current branch; git_status reports it
write("d.txt", "z\n")
out4 = git_utils.git_commit.invoke({"repo_file_path": "repo", "commit_message": "follow-up"})
assert out4 == "Pushed follow-up commit to 'forge-upgrade-20250115-1200'.", out4
assert "No changes to commit" in git_utils.git_commit.invoke({"repo_file_path": "repo", "commit_message": "noop"})
write("e.txt", "w\n")
status = git_utils.git_status.invoke({"repo_file_path": "repo"})
assert "branch: forge-upgrade-20250115-1200 (tracks origin/forge-upgrade-20250115-1200)" in status and "uncommitted changes: 1" in status and "?? e.txt" in status, status
print("1e ok:\n" + status)

# ---------- 2. Maven summary + baseline (subprocess faked) ----------
import agent
fake_out = {}
_real_run = subprocess.run
def fake_run(cmd, **kw):
    if not cmd or not os.path.basename(cmd[0]).lower().startswith('mvn'):
        return _real_run(cmd, **kw)
    pom = cmd[cmd.index("-f") + 1]
    text, code = fake_out[os.path.realpath(os.path.dirname(pom))]
    return subprocess.CompletedProcess(cmd, code, stdout=text, stderr="")
agent.subprocess.run = fake_run
mod = os.path.join(tmp, "repo", "mod"); os.makedirs(mod); open(os.path.join(mod, "pom.xml"), "w").write("<project/>")
before = """[INFO] Tests run: 12, Failures: 1, Errors: 1, Skipped: 0, Time elapsed: 2.1 s <<< FAILURE! -- in org.example.DaoTest
[ERROR] org.example.DaoTest.loadsRows -- Time elapsed: 0.5 s <<< ERROR!
org.h2.jdbc.JdbcSQLNonTransientConnectionException: Database may be already in use
[ERROR] testCount(org.example.DaoTest)  Time elapsed: 0.1 s  <<< FAILURE!
[INFO] Reactor Summary for ams-common 1.0:
[INFO] AssetManagementSharedCommon ........................ SUCCESS [  3.1 s]
[INFO] AssetManagementSharedServices ...................... FAILURE [  9.0 s]
[INFO] Tests run: 12, Failures: 1, Errors: 1, Skipped: 0
[INFO] BUILD FAILURE
"""
mod = os.path.realpath(mod); fake_out[mod] = (before, 1)
base = agent.run_maven_test.invoke({"code_dir": "repo/mod", "baseline": True})
assert "FAILED (exit 1)" in base and "tests: run 12, failures 1, errors 1" in base, base
assert "failing tests: DaoTest.loadsRows, DaoTest.testCount" in base, base
assert "reactor: AssetManagementSharedCommon: SUCCESS; AssetManagementSharedServices: FAILURE" in base, base
assert "baseline recorded: 2 failing test(s) BEFORE the migration" in base, base
print("2a ok (baseline):\n" + base.split("--- log tail ---")[0])

after = before.replace("[ERROR] testCount(org.example.DaoTest)  Time elapsed: 0.1 s  <<< FAILURE!\n",
                       "[ERROR] org.example.NewTest.breaks -- Time elapsed: 0.5 s <<< FAILURE!\n")
fake_out[mod] = (after, 1)
res = agent.run_maven_test.invoke({"code_dir": "repo/mod"})
assert "vs baseline: 1 new failure(s): NewTest.breaks; 1 pre-existing (failed before the migration too): DaoTest.loadsRows; 1 fixed" in res, res
assert "ACTION: the new failures are regressions" in res
mod = os.path.realpath(mod); fake_out[mod] = (before, 1)
res2 = agent.run_maven_test.invoke({"code_dir": "repo/mod"})
assert "0 new failure(s); 2 pre-existing" in res2 and "ACTION: no regressions" in res2, res2
fake_out[mod] = ("[INFO] Tests run: 12, Failures: 0, Errors: 0, Skipped: 1\n[INFO] BUILD SUCCESS\n", 0)
res3 = agent.run_maven_test.invoke({"code_dir": "repo/mod"})
assert "SUCCESS" in res3 and "0 new failure(s); 0 pre-existing (failed before the migration too); 2 fixed" in res3, res3
print("2b ok (regression / pre-existing / fixed classification)")
assert len(res3) < settings.MAVEN_OUTPUT_MAX_CHARS + 600
B = os.path.join(HERE, "fixtures", "maven")
common = open(os.path.join(B, "ams-common-test.log"), encoding="utf-8").read(); internal = open(os.path.join(B, "ams-internal-test.log"), encoding="utf-8").read()
fake_out[mod] = (common, 1)
real = agent.run_maven_test.invoke({"code_dir": "repo/mod", "baseline": True})
head = real.split("--- log tail ---")[0]
assert "tests: run 147, failures 0, errors 1, skipped 0" in head, head
assert "failing tests: ConfigServiceImplTest.allPropertiesComeBackKeyedByPropertyKey" in head, head
assert "reactor: AMS Common: SUCCESS; Asset Management Shared Common: SUCCESS; Asset Management Network Validation: SUCCESS; Asset Management Shared Services: FAILURE" in head, head
assert "baseline recorded: 1 failing test(s)" in head, head
print("2c ok (real ams-common log):\n" + head)
fake_out[mod] = (internal, 1)
real2 = agent.run_maven_test.invoke({"code_dir": "repo/mod", "baseline": True})
head2 = real2.split("--- log tail ---")[0]
assert "compile errors: 4" in head2 and "pom: 'dependencies.dependency.version' for javax.servlet:javax.servlet-api:jar is missing. @ line 135, column 17" in head2, head2
assert "no tests ran: the build failed before the test phase" in head2 and "does not reach the tests" in head2, head2
print("2d ok (real ams-internal log):\n" + head2)
fake_out[mod] = ("[ERROR] /x/y/src/main/java/org/example/Foo.java:[12,5] cannot find symbol\n[ERROR]   symbol: class HttpServletRequest\n[INFO] BUILD FAILURE\n", 1)
real3 = agent.run_maven_test.invoke({"code_dir": "repo/mod"})
assert "compile errors: 1" in real3 and "Foo.java:12 cannot find symbol" in real3 and "nothing here is known to be pre-existing" in real3, real3
print("2e ok (compile error after an unbuildable baseline)")

# ---------- 3. step limit -> StepLimitReached with the partial transcript ----------
os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
import chat
from langgraph.errors import GraphRecursionError
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
class FakeAgent:
    def stream(self, messages):
        msgs = list(messages)
        msgs.append(AIMessage("", tool_calls=[{"name": "run_maven_test", "args": {"code_dir": "repo"}, "id": "1"}]))
        yield {"messages": list(msgs)}
        msgs.append(ToolMessage("mvn ok", tool_call_id="1", name="run_maven_test"))
        msgs.append(AIMessage("Half way through the plan."))
        yield {"messages": list(msgs)}
        raise GraphRecursionError("Recursion limit of 400 reached")
seen = []
try:
    chat.stream_turn(FakeAgent(), [HumanMessage("go")], on_tool_call=lambda n, a: seen.append(("call", n)),
                     on_tool_result=lambda n, p: seen.append(("result", n)), on_text=lambda t: seen.append(("text", t)))
    raise AssertionError("no StepLimitReached")
except chat.StepLimitReached as e:
    assert len(e.messages) == 4 and e.messages[-1].content == "Half way through the plan.", e.messages
    assert seen == [("call", "run_maven_test"), ("result", "run_maven_test"), ("text", "Half way through the plan.")], seen
    print("3 ok: step limit surfaces as StepLimitReached with", len(e.messages), "messages and the callbacks fired")

# ---------- 4. prompt still formats ----------
import inspect, re
src = inspect.getsource(agent.Claude.create_prompt)
keys = set(re.findall(r"(?<!\{)\{(\w+)\}(?!\})", agent.PROMPT_TEMPLATE))
print("4 prompt placeholders:", sorted(keys))
missing = [k for k in keys if k + "=" not in src and f'"{k}"' not in src]
assert not missing, missing
print("ALL OK")
