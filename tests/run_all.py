"""Run every offline test suite and print a summary.

    python tests/run_all.py            (macOS / Linux)
    py tests\\run_all.py               (Windows)

No AWS or GitHub access is needed: Bedrock, GitHub and Maven are stubbed and git
runs against local repositories in a temp dir. Requires git on PATH and the
project's virtualenv (python -m pip install -r python/requirements.txt).
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SUITES = ["test_fixes", "test_inventory", "test_keys", "test_gate", "test_pii", "test_queue", "test_windows"]


def main():
    failed = []
    for name in SUITES:
        started = time.time()
        proc = subprocess.run([sys.executable, os.path.join(HERE, f"{name}.py")], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              env=dict(os.environ, PYTHONUTF8="1", STREAMLIT_SERVER_HEADLESS="true"))
        ok = proc.returncode == 0 and "ALL OK" in proc.stdout
        checks = sum(1 for line in proc.stdout.splitlines() if " ok" in line[:6] or line[:3].strip().isdigit())
        print(f"{'PASS' if ok else 'FAIL'}  {name:16s} {time.time() - started:5.1f}s")
        if not ok:
            failed.append(name)
            tail = (proc.stdout + proc.stderr).strip().splitlines()[-15:]
            print("      " + "\n      ".join(tail))
    print(f"\n{len(SUITES) - len(failed)} of {len(SUITES)} suites passed" + (f"; failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
