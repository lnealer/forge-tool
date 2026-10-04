"""File inventory, acceptance checks, approval freeze, reviewer message.

Offline: no AWS calls (Bedrock and GitHub are stubbed), git runs against local
bare repositories in a temp dir. Run directly or through tests/run_all.py.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON_DIR = os.path.join(os.path.dirname(HERE), "python")
sys.path.insert(0, PYTHON_DIR)
os.chdir(PYTHON_DIR)
import os, subprocess, sys, tempfile
import workdir, ui_events, techstack, settings

def sh(args, cwd): subprocess.run(args, cwd=cwd, check=True, capture_output=True)
def w(root, rel, text):
    path = os.path.join(root, rel); os.makedirs(os.path.dirname(path), exist_ok=True); open(path, "w").write(text)

tmp = tempfile.mkdtemp(); workdir.set_working_dir(tmp)
remote = os.path.join(tmp, "remote.git"); sh(["git", "init", "-q", "--bare", "-b", settings.GIT_SOURCE_BRANCH or "main", remote], tmp)
repo = os.path.join(tmp, "repo"); os.makedirs(repo)
sh(["git", "init", "-q", "-b", settings.GIT_SOURCE_BRANCH or "main"], repo)
w(repo, "web/src/main/java/a/Filter1.java", "import javax.servlet.Filter;\nimport javax.sql.DataSource;\nclass Filter1 {}\n")
w(repo, "web/src/main/java/a/Action1.java", "import javax.servlet.http.HttpSession;\nimport com.opensymphony.xwork2.Action;\nimport org.springframework.stereotype.Component;\nclass Action1 {}\n")
w(repo, "web/src/main/java/a/Clean.java", "import org.springframework.stereotype.Service;\nclass Clean {}\n")
w(repo, "web/src/main/java/a/Sec.java", "@EnableGlobalMethodSecurity(prePostEnabled = true)\nclass Sec {}\n")
w(repo, "web/src/test/java/a/OldTest.java", "import org.junit.Test;\nimport org.springframework.test.context.ContextConfiguration;\nclass OldTest {}\n")
w(repo, "web/src/main/java/a/Pw.java", 'class Pw { String pass' + 'word = "Sup3rSecret!"; }\nimport javax.servlet.ServletContext;\n')
sh(["git", "add", "-A"], repo); sh(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], repo)
sh(["git", "remote", "add", "origin", remote], repo); sh(["git", "push", "-q", "origin", "HEAD"], repo); sh(["git", "fetch", "-q", "origin"], repo)

import agent
ids = ["springsec-to-springsec6", "javax-to-jakarta", "struts2-modernize", "junit4-to-junit5", "spring-to-spring6"]
packs, unknown = techstack.resolve_packs(ids + ["no-such-pack"])
order = [p["id"] for p in packs]
assert unknown == ["no-such-pack"], unknown
assert order.index("javax-to-jakarta") < order.index("spring-to-spring6") < order.index("springsec-to-springsec6"), order
print("1 ok: resolve_packs orders by depends_on and reports unknown ids:", order)

r = techstack.Repo(repo)
inv, notes = techstack.build_inventory(r, packs)
def packs_of(f, must=True): return [x["pack"] for x in inv.get(f, []) if x["must_change"] == must]
assert packs_of("web/src/main/java/a/Action1.java") == ["javax-to-jakarta", "struts2-modernize"], inv["web/src/main/java/a/Action1.java"]
assert packs_of("web/src/main/java/a/Action1.java", False) == ["spring-to-spring6"]
assert packs_of("web/src/main/java/a/Clean.java") == [] and packs_of("web/src/main/java/a/Clean.java", False) == ["spring-to-spring6"]
assert packs_of("web/src/main/java/a/Sec.java") == ["springsec-to-springsec6"]
# spring pack (include_tests unset) must NOT select the test file; junit (test glob) must
assert packs_of("web/src/test/java/a/OldTest.java") == ["junit4-to-junit5"] and "spring-to-spring6" not in [x["pack"] for x in inv["web/src/test/java/a/OldTest.java"]]
print("2 ok: one entry per file, packs in order, must-change vs verify-only, test filtering")

out = agent.list_migration_files.invoke({"repo_dir": "repo", "pack_ids": ",".join(ids)})
assert "MUST CHANGE: 5 file(s)" in out and "a/Action1.java  <- javax-to-jakarta, struts2-modernize  (verify: spring-to-spring6)" in out, out
assert "VERIFY ONLY: 1 file(s)" in out, out
print("3 ok: list_migration_files output\n" + "\n".join("   " + l for l in out.splitlines()[:6]))

res = agent.check_pack_acceptance.invoke({"repo_dir": "repo", "pack_ids": ",".join(ids)})
assert "javax-to-jakarta: 3 leftover(s)" in res and "Action1.java:1: import javax.servlet.http.HttpSession;" in res, res
assert "Sup3rSecret" not in res, "secret leaked in leftover text"
assert "manual: authz_parity: for the human reviewer" in res and "test_parity" not in res.split("junit4-to-junit5")[1].split("\n\n")[0].replace("junit4", ""), res
assert "count_unchanged" in res and "(unchanged)" in res and "spring-to-spring6: CLEAN" in res, res
print("4 ok: leftovers as file:line, secrets redacted, manual checks listed, count_unchanged compared with the source branch")

# migrate javax, but also (wrongly) rewrite a JDK javax.sql import -> count_unchanged must flag it
w(repo, "web/src/main/java/a/Filter1.java", "import jakarta.servlet.Filter;\nimport jakarta.sql.DataSource;\nclass Filter1 {}\n")
res = agent.check_pack_acceptance.invoke({"repo_dir": "repo", "pack_ids": "javax-to-jakarta"})
assert "(CHANGED)" in res, res
assert "not finished: javax-to-jakarta" in res, res
print("5 ok: count_unchanged catches a JDK javax.* import that was wrongly renamed")

# conditional rule (when container=tomcat) is listed, not evaluated
pack = {"id": "x", "acceptance": [{"no_match": "liberty", "scope": "**/pom.xml", "when": {"container": "tomcat"}}, {"build": "mvn -q package"}]}
rules, manual = techstack._acceptance_rules(pack)
assert rules == [] and "applies only when container=tomcat" in manual[0] and "covered by the Maven" in manual[1], manual
print("6 ok: conditional and build rules listed as manual")

# approval freezes the pack set
ui_events.approve_packs([]); ui_events.set_proposed_packs(["javax-to-jakarta", "spring-to-spring6"])
assert ui_events.plan_packs() == (["javax-to-jakarta", "spring-to-spring6"], "proposed plan (not approved yet)")
ui_events.approve_packs(["javax-to-jakarta", "", "javax-to-jakarta", "springsec-to-springsec6"])
assert ui_events.plan_packs() == (["javax-to-jakarta", "springsec-to-springsec6"], "approved plan")
ui_events.set_proposed_packs(["junit4-to-junit5"])  # a later re-proposal does not override the approval
out = agent.list_migration_files.invoke({"repo_dir": "repo"})
assert out.startswith("Migration inventory from the approved plan: 2 pack(s)"), out[:120]
print("7 ok: approved pack set is frozen and used when no pack_ids are passed")

# reviewer message
import reviewer
msg = reviewer.build_review_message("a/B.java", "-import javax.x;\n+import jakarta.x;", "Java 21", "package a;\nimport jakarta.x;\nclass B {}\n")
assert "File after the migration:\n```java\npackage a;" in msg and "Unified diff against the source branch" in msg
old = settings.REVIEW_MAX_DIFF_CHARS; settings.REVIEW_MAX_DIFF_CHARS = 100
big = reviewer.build_review_message("a/B.java", "d" * 50, "Java 21", "x" * 10000)
assert "[file truncated for review]" in big and big.count("x") <= 4000 + 10
settings.REVIEW_MAX_DIFF_CHARS = old
assert "speculative" in reviewer.REVIEW_SYSTEM_PROMPT and "import-only" in reviewer.REVIEW_SYSTEM_PROMPT
print("8 ok: reviewer gets the whole file + diff, truncation marker, evidence-only rubric")
print("ALL OK")
