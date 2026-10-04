"""Work queue order and progress, user waivers.

Offline: no AWS calls (Bedrock and GitHub are stubbed), git runs against local
bare repositories in a temp dir. Run directly or through tests/run_all.py.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON_DIR = os.path.join(os.path.dirname(HERE), "python")
sys.path.insert(0, PYTHON_DIR)
os.chdir(PYTHON_DIR)
import os, sys, tempfile, subprocess
import workdir, ui_events, settings
def w(root, rel, text):
    p = os.path.join(root, rel); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "w").write(text)
tmp = tempfile.mkdtemp(); workdir.set_working_dir(tmp); repo = os.path.join(tmp, "repo")
w(repo, "web/src/test/java/a/ATest.java", "import javax.servlet.http.HttpServletRequest;\nimport org.junit.Test;\nclass ATest {}\n")
w(repo, "web/src/main/java/a/Action.java", "import javax.servlet.http.HttpSession;\nimport com.opensymphony.xwork2.Action;\nclass A {}\n")
w(repo, "web/src/main/java/a/Filter.java", "import javax.servlet.Filter;\nclass F {}\n")
w(repo, "web/src/main/webapp/home.jsp", '<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>\n')
w(repo, "web/src/main/java/a/Ejb.java", "import javax.ejb.Stateless;\n@Stateless class E {}\n")
import agent, chat
ui_events.approve_packs(["junit4-to-junit5", "jsp-jstl-modernize", "struts2-modernize", "javax-to-jakarta", "ejb3-to-spring"])
out = agent.next_migration_files.invoke({"repo_dir": "repo", "limit": 3})
lines = [l.strip() for l in out.splitlines() if "<-" in l]
assert out.startswith("Progress: 0 of 5 files done (5 left)"), out
assert lines[0].startswith("web/src/main/java/a/Action.java  <- javax-to-jakarta, struts2-modernize"), lines
assert lines[1].startswith("web/src/main/java/a/Ejb.java  <- javax-to-jakarta") and "ejb3" not in lines[1], lines   # detect-only pack not applied
assert lines[2].startswith("web/src/main/java/a/Filter.java"), lines
full = agent.next_migration_files.invoke({"repo_dir": "repo", "limit": 20})
order = [l.strip().split()[0] for l in full.splitlines() if "<-" in l]
assert order.index("web/src/test/java/a/ATest.java") > order.index("web/src/main/java/a/Filter.java"), order
assert "only the user can take a file or pack out of scope" in out
print("1 ok: main code first, then webapp, then tests; packs in dependency order; detect-only packs not queued; progress 0/5")

w(repo, "web/src/main/java/a/Filter.java", "import jakarta.servlet.Filter;\nclass F {}\n")
w(repo, "web/src/main/java/a/Ejb.java", "import jakarta.ejb.Stateless;\n@Stateless class E {}\n")
out = agent.next_migration_files.invoke({"repo_dir": "repo"})
assert out.startswith("Progress: 2 of 5 files done (3 left)"), out
print("2 ok: progress advances as files stop being MUST CHANGE")

ui_events.waive_pack("jsp-jstl-modernize")
out = agent.next_migration_files.invoke({"repo_dir": "repo", "limit": 20})
assert "home.jsp" not in out, out
print("3 ok: a pack the user accepted is no longer queued")

for rel, text in {"web/src/main/java/a/Action.java": "import jakarta.servlet.http.HttpSession;\nimport org.apache.struts2.action.Action;\nclass A {}\n",
                  "web/src/test/java/a/ATest.java": "import jakarta.servlet.http.HttpServletRequest;\nimport org.junit.jupiter.api.Test;\nclass ATest {}\n"}.items():
    w(repo, rel, text)
out = agent.next_migration_files.invoke({"repo_dir": "repo"})
assert "No MUST CHANGE files left" in out, out
print("4 ok: empty queue says so and points to acceptance and tests")

sent = []; chat._queue_turn = lambda m: sent.append(m)
import streamlit as st
st.session_state.gate_refusal = {"unfinished": [("javax-to-jakarta", 21), ("struts2-modernize", 25)], "failures": ["x"]}
chat._accept_pack("struts2-modernize")
assert "struts2-modernize" in ui_events.waived_packs() and st.session_state.gate_refusal["unfinished"] == [("javax-to-jakarta", 21)]
chat._finish_packs(["javax-to-jakarta"])
assert "accepts struts2-modernize as not migrated" in sent[0] and "next_migration_files until it is empty" in sent[1], sent
print("5 ok: chat Accept records the waiver and updates the panel; Finish queues the right instruction")
print("ALL OK")
