"""Windows-specific behaviour, simulated so it runs on any OS.

Maven is resolved through shutil.which (mvn.cmd on Windows), git output is
decoded as UTF-8 regardless of the code page, and clones on Windows get
core.autocrlf / core.longpaths.
"""
import os
import sys
import subprocess
import tempfile
import importlib

HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON_DIR = os.path.join(os.path.dirname(HERE), "python")
sys.path.insert(0, PYTHON_DIR)
os.chdir(PYTHON_DIR)

import shutil
import workdir
tmp = tempfile.mkdtemp(); workdir.set_working_dir(tmp)

# 1. Maven resolved through shutil.which and used as argv[0]
real_which = shutil.which
shutil.which = lambda name, *a, **k: r"C:\tools\maven\bin\mvn.cmd" if name == "mvn" else real_which(name, *a, **k)
import agent
importlib.reload(agent)
shutil.which = real_which
assert agent._MVN == r"C:\tools\maven\bin\mvn.cmd", agent._MVN
seen = {}
real_run = subprocess.run
def fake_run(cmd, *a, **kw):
    if cmd and cmd[0] == agent._MVN:
        seen["cmd"], seen["kw"] = cmd, kw
        return subprocess.CompletedProcess(cmd, 0, stdout="[INFO] Tests run: 1, Failures: 0, Errors: 0, Skipped: 0\n", stderr="")
    return real_run(cmd, *a, **kw)
agent.subprocess.run = fake_run
os.makedirs(os.path.join(tmp, "m")); open(os.path.join(tmp, "m", "pom.xml"), "w").write("<project/>")
agent.run_maven_test.invoke({"code_dir": "m"})
agent.subprocess.run = real_run
assert seen["cmd"][0] == r"C:\tools\maven\bin\mvn.cmd" and seen["kw"].get("encoding") == "utf-8" and seen["kw"].get("errors") == "replace", seen
print("1 ok: Maven is launched through the shutil.which result (mvn.cmd on Windows), output decoded as UTF-8")

# 2. git output with non-ASCII text decodes as UTF-8
import git_utils
repo = os.path.join(tmp, "g"); os.makedirs(repo)
real_run(["git", "init", "-q"], cwd=repo, check=True)
real_run(["git", "-c", "user.name=Zoë Müller", "-c", "user.email=z@example.com", "commit", "-q", "--allow-empty",
          "-m", "Migración: ñ ü ß 日本"], cwd=repo, check=True)
ok, out = git_utils._run_git(["log", "-1", "--format=%an %s"], repo)
assert ok and out == "Zoë Müller Migración: ñ ü ß 日本", out
print("2 ok: git output with non-ASCII text decodes as UTF-8")

# 3. Clones on Windows get autocrlf + longpaths (Repo.clone_from stubbed)
captured = {}
class FakeRepo:
    class _Cfg:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def set_value(self, *a): pass
    class active_branch: name = "main"
    def config_writer(self): return FakeRepo._Cfg()
def fake_clone(url, target, **kw):
    captured.update(kw); os.makedirs(target, exist_ok=True); return FakeRepo()
git_utils.configure_github_token("x" * 40)
real_clone, real_name = git_utils.Repo.clone_from, os.name
git_utils.Repo.clone_from = staticmethod(fake_clone)
try:
    os.name = "nt"
    git_utils.clone_repo.invoke({"url": "https://github.com/o/r.git", "repo_dir": "winrepo"})
finally:
    os.name = real_name
    git_utils.Repo.clone_from = real_clone
assert captured.get("multi_options") == ["--config core.autocrlf=true", "--config core.longpaths=true"] and captured.get("allow_unsafe_options") is True, captured
print("3 ok: clones on Windows get core.autocrlf=true and core.longpaths=true")
print("ALL OK")
