"""PR gate, generated verification section, test parity, draft mode, reviewer guidance.

Offline: no AWS calls (Bedrock and GitHub are stubbed), git runs against local
bare repositories in a temp dir. Run directly or through tests/run_all.py.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON_DIR = os.path.join(os.path.dirname(HERE), "python")
sys.path.insert(0, PYTHON_DIR)
os.chdir(PYTHON_DIR)
import os, re, subprocess, sys, tempfile, json
import workdir, ui_events, settings, techstack, guardrails

def sh(args, cwd): return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout
def w(root, rel, text):
    path = os.path.join(root, rel); os.makedirs(os.path.dirname(path), exist_ok=True); open(path, "w").write(text)

tmp = tempfile.mkdtemp(); workdir.set_working_dir(tmp)
src = settings.GIT_SOURCE_BRANCH or "main"
remote = os.path.join(tmp, "remote.git"); sh(["git", "init", "-q", "--bare", "-b", src, remote], tmp)
repo = os.path.realpath(os.path.join(tmp, "repo")); os.makedirs(repo); sh(["git", "init", "-q", "-b", src], repo)
sh(["git", "config", "user.name", "t"], repo); sh(["git", "config", "user.email", "t@t"], repo)
w(repo, "pom.xml", "<project><artifactId>demo</artifactId></project>")
w(repo, "src/main/java/demo/Calc.java", "package demo;\nclass Calc { static int add(int a,int b){return a+b;} static boolean reject(int x){return x<0;} }\n")
BEFORE = '''package demo;
import org.junit.Test;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;
public class CalcTest {
    @Test
    public void addsNumbers() {
        assertEquals("sum", 4, Calc.add(2, 2));
    }
    @Test
    public void rejectsNegative() {
        assertTrue("negative must be rejected", Calc.reject(-1));
    }
}
'''
SYNTAX_ONLY = '''package demo;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
public class CalcTest {
    @Test
    public void addsNumbers() {
        assertEquals(4, Calc.add(2, 2), "sum");
    }
    @Test
    public void rejectsNegative() {
        assertTrue(Calc.reject(-1),
                "negative must be rejected");
    }
}
'''
REWRITTEN = SYNTAX_ONLY.replace("rejectsNegative", "acceptsNegative").replace("Calc.reject(-1),\n                \"negative must be rejected\"", "true, \"negative is fine\"")
w(repo, "src/test/java/demo/CalcTest.java", BEFORE)
sh(["git", "add", "-A"], repo); sh(["git", "commit", "-qm", "init"], repo)
sh(["git", "remote", "add", "origin", remote], repo); sh(["git", "push", "-q", "origin", src], repo); sh(["git", "fetch", "-q", "origin"], repo)

import agent, git_utils, reviewer
MVN = {"out": "", "code": 0}
real_run = subprocess.run
def fake_run(cmd, *a, **kw):
    if cmd and os.path.basename(cmd[0]).lower().startswith("mvn"):
        return subprocess.CompletedProcess(cmd, MVN["code"], stdout=MVN["out"], stderr="")
    return real_run(cmd, *a, **kw)
agent.subprocess.run = fake_run
OK_OUT = "[INFO] Tests run: 2, Failures: 0, Errors: 0, Skipped: 0\n[INFO] BUILD SUCCESS\n"
COMPILE_ERR = "[ERROR] /x/src/test/java/demo/CalcTest.java:[9,9] cannot find symbol\n[INFO] BUILD FAILURE\n"
NEW_FAIL = ("[ERROR] Tests run: 2, Failures: 1, Errors: 0, Skipped: 0\n"
            "[ERROR] demo.CalcTest.addsNumbers -- Time elapsed: 0.1 s <<< FAILURE!\n[INFO] BUILD FAILURE\n")
sent = []
git_utils.open_pull_request = lambda url, br, title, body, draft=False: sent.append({"title": title, "body": body, "draft": draft}) or "PR created"
def pr(desc="Migration.", branch=None):
    return agent.create_pull_request.invoke({"github_url": "https://github.com/x/y.git", "branch_name": branch, "pr_title": "T", "pr_description": desc})
def test(baseline=False):
    return agent.run_maven_test.invoke({"code_dir": "repo", "baseline": baseline})

ui_events.approve_packs(["junit4-to-junit5"])
MVN.update(out=OK_OUT, code=0); test(baseline=True)

# migrate: syntax-only test change + a new env read in main code, commit and push
w(repo, "src/test/java/demo/CalcTest.java", SYNTAX_ONLY)
w(repo, "src/main/java/demo/Calc.java", "package demo;\nclass Calc { static final String K = System.getenv(\"CALC_KEY\"); static int add(int a,int b){return a+b;} static boolean reject(int x){return x<0;} }\n")
out = git_utils.create_branch.invoke({"repo_file_path": "repo", "commit_message": "migrate"})
branch = re.search(r"'([^']+)'", out).group(1)

r = pr(branch=branch)
assert r.startswith("ERROR: PR gate refused") and "no test run after the migration" in r and not sent, r
print("1 ok: refused without a test run after the migration")

MVN.update(out=COMPILE_ERR, code=1); test()
r = pr(branch=branch); assert "does not compile" in r and not sent, r
print("2 ok: refused when the final run does not compile")

MVN.update(out=NEW_FAIL, code=1); test()
r = pr(branch=branch); assert "1 new test failure(s) vs the baseline: CalcTest.addsNumbers" in r, r
print("3 ok: refused on a new failure vs the baseline")

MVN.update(out=OK_OUT, code=0); test()
w(repo, "src/main/java/demo/Extra.java", "class Extra {}\n")
r = pr(branch=branch)
assert "uncommitted changes" in r and "older than the current code" in r, r
os.remove(os.path.join(repo, "src/main/java/demo/Extra.java"))
print("4 ok: refused on uncommitted changes / tests older than the code")

r = pr(branch=branch)
assert r == "PR created" and len(sent) == 1 and sent[0]["draft"] is False, (r, sent)
body = sent[0]["body"]
assert "## Verification (generated by forge from tool results)" in body and "| `.` | 0 failing | 2 run, 0 failing, 0 new |" in body, body
assert "| `junit4-to-junit5` | CLEAN |" in body and "- `CALC_KEY`" in body, body
print("5 ok: gate passes; generated section has the test table, pack status and CALC_KEY")

# test rewritten -> parity
sent.clear()
w(repo, "src/test/java/demo/CalcTest.java", REWRITTEN)
guardrails.drain_manual_reviews()
p = agent.check_test_parity.invoke({"repo_dir": "repo"})
q = guardrails.drain_manual_reviews()
assert "removed or renamed tests: rejectsNegative" in p and "changed expectations: 'negative must be rejected'" in p, p
assert len(q) == 1 and q[0]["path"] == "src/test/java/demo/CalcTest.java" and q[0]["issues"][0]["severity"] == "high", q
git_utils.git_commit.invoke({"repo_file_path": "repo", "commit_message": "rewrite"})
MVN.update(out=OK_OUT, code=0); test()
r = pr(branch=branch)
assert "test parity: src/test/java/demo/CalcTest.java" in r and "pack junit4-to-junit5 is approved but unfinished: 1 leftover(s)" in r, r
ui_events.approve_file("src/test/java/demo/CalcTest.java")
r = pr(branch=branch); assert r == "PR created" and "Approved as-is by the user" in sent[-1]["body"], r
print("6 ok: a rewritten test is flagged, queued for review, blocks the PR until the user approves it")

# unclean pack must be named in the description
sent.clear(); ui_events._APPROVED_FILES.clear()
w(repo, "src/test/java/demo/OldTest.java", "package demo;\nimport org.junit.Test;\npublic class OldTest { @Test public void t() {} }\n")
git_utils.git_commit.invoke({"repo_file_path": "repo", "commit_message": "old"}); test()
ui_events.approve_file("src/test/java/demo/CalcTest.java")
r = pr(branch=branch); assert "pack junit4-to-junit5 is approved but unfinished: 1 leftover(s)" in r, r
r = pr("Detected but not migrated: junit4-to-junit5 - OldTest.java kept on JUnit 4 (reason).", branch=branch)
assert "approved but unfinished" in r, "naming the pack in the description must no longer pass"
ref = ui_events.drain_gate_refusals(); assert ref and ref[-1]["unfinished"] == [("junit4-to-junit5", 1)], ref
ui_events.waive_pack("junit4-to-junit5")
r = pr("Detected but not migrated: junit4-to-junit5 - OldTest.java kept on JUnit 4 (user accepted).", branch=branch)
assert r == "PR created" and "Not migrated (accepted by the user): `junit4-to-junit5`" in sent[-1]["body"], r
ui_events._WAIVED_PACKS.clear()
print("7 ok: an unfinished approved pack blocks even when named in the description; only a user waiver lets it through")

# draft and off modes
sent.clear(); ui_events._APPROVED_FILES.clear()
settings.PR_GATE = "draft"; r = pr(branch=branch)
assert sent[-1]["draft"] is True and "PR gate failed - this is a draft" in sent[-1]["body"], sent[-1]["body"][:300]
settings.PR_GATE = "off"; pr(branch=branch)
assert "Verification (generated" not in sent[-1]["body"] and sent[-1]["draft"] is False
settings.PR_GATE = "block"
print("8 ok: draft mode opens a draft with the open items; off mode skips the gate")

# GitHubProvider sends draft
import requests
posted = {}
class R: status_code = 201; text = "{}"; json = lambda self: {"html_url": "u"}
requests.post = lambda url, json=None, headers=None, timeout=None: posted.update(json) or R()
git_utils.GitHubProvider("t", "https://api.github.com/repos/x/y").create_pull_request("b", "t", "d", draft=True)
assert posted["draft"] is True
print("9 ok: GitHub request carries draft")

# reviewer gets pack guidance
msg = reviewer.build_review_message("A.java", "-x\n+y", "Java 21", "class A {}", [("javax-to-jakarta", techstack.pack_guidance(techstack.resolve_packs(["javax-to-jakarta"])[0][0]))])
assert "Pack guidance for this file" in msg and "### Pack javax-to-jakarta" in msg and "Rule 1" in msg, msg[:400]
assert "there is no jakarta.sql" in reviewer.REVIEW_SYSTEM_PROMPT
print("10 ok: reviewer message carries the pack's transform guidance; rubric names the known traps")
print("ALL OK")
