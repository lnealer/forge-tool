"""Bedrock personal-data scan with a stubbed client.

Offline: no AWS calls (Bedrock and GitHub are stubbed), git runs against local
bare repositories in a temp dir. Run directly or through tests/run_all.py.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON_DIR = os.path.join(os.path.dirname(HERE), "python")
sys.path.insert(0, PYTHON_DIR)
os.chdir(PYTHON_DIR)
import os, sys, tempfile, json
import settings, workdir, ui_events, guardrails, pii_scan

def w(root, rel, text):
    p = os.path.join(root, rel); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "w").write(text)
VALUES = {"NAME": "Maria Gonzalez", "EMAIL": "mgonzalez@contoso.com", "PHONE": "(415) 555-0132", "US_SOCIAL_SECURITY_NUMBER": "536-22-8726"}
class FakeBedrock:
    calls = []
    def apply_guardrail(self, **kw):
        text = kw["content"][0]["text"]["text"]
        FakeBedrock.calls.append(len(text))
        ents = [{"type": t, "match": v, "action": "ANONYMIZED", "detected": True} for t, v in VALUES.items() if v in text]
        return {"assessments": [{"sensitiveInformationPolicy": {"piiEntities": ents}}],
                "usage": {"sensitiveInformationPolicyUnits": -(-len(text) // 1000)}}
pii_scan._client = FakeBedrock()
settings.PII_SCAN_GUARDRAIL_ID, settings.PII_SCAN_GUARDRAIL_VERSION = "fake", "1"

tmp = tempfile.mkdtemp(); workdir.set_working_dir(tmp); repo = os.path.join(tmp, "repo")
filler = "".join(f"-- filler line {i}\n" for i in range(1500))            # ~30k chars -> several chunks
w(repo, "db/seed.sql", filler + f"INSERT INTO customer VALUES ('{VALUES['NAME']}', '{VALUES['EMAIL']}');\n" + filler + f"-- ssn {VALUES['US_SOCIAL_SECURITY_NUMBER']}\n")
w(repo, "src/test/resources/app.properties", f"support.phone={VALUES['PHONE']}\nsupport.phone.backup={VALUES['PHONE']}\n")
w(repo, "src/main/java/demo/Main.java", f"class Main {{ String owner = \"{VALUES['NAME']}\"; }}\n")   # not in the default globs
w(repo, "target/classes/leak.properties", f"x={VALUES['EMAIL']}\n")                                  # build dir: skipped

r = pii_scan.scan_tree(repo)
got = {(f.path, f.line, f.kind) for f in r["findings"]}
expect = {("db/seed.sql", 1501, "Personal data: NAME"), ("db/seed.sql", 1501, "Personal data: EMAIL"),
          ("db/seed.sql", 3002, "Personal data: US_SOCIAL_SECURITY_NUMBER"),
          ("src/test/resources/app.properties", 1, "Personal data: PHONE"), ("src/test/resources/app.properties", 2, "Personal data: PHONE")}
assert got == expect, sorted(got)
assert max(FakeBedrock.calls) <= settings.PII_SCAN_CHUNK_CHARS and len(FakeBedrock.calls) >= 4, FakeBedrock.calls
assert r["files"] == 2 and r["text_units"] > 0 and not r["truncated"] and not r["error"], r
print(f"1 ok: 5 findings at the right lines across {len(FakeBedrock.calls)} chunks; main Java and target/ not scanned")

n = len(FakeBedrock.calls); pii_scan.scan_tree(repo)
assert len(FakeBedrock.calls) == n, "identical chunks must not be re-sent"
print("2 ok: unchanged files are not paid for twice (content cache)")

settings.PII_SCAN_MAX_CHARS = 12000; pii_scan._CACHE.clear()
r2 = pii_scan.scan_tree(repo); assert r2["truncated"] and r2["scanned_chars"] <= 12000, r2
settings.PII_SCAN_MAX_CHARS = 1500000
print("3 ok: the budget stops the scan and says so")

class Boom:
    def apply_guardrail(self, **kw): raise RuntimeError("AccessDenied")
pii_scan._client, saved = Boom(), pii_scan._client; pii_scan._CACHE.clear()
r3 = pii_scan.scan_tree(repo); assert r3["error"].startswith("Bedrock PII scan unavailable: RuntimeError") and r3["findings"] == []
pii_scan._client = saved
print("4 ok: a Bedrock error degrades to a note")

import agent
ui_events.drain_secret_blocks(); pii_scan._CACHE.clear()
out = agent.scan_for_secrets.invoke({"path": "repo"})
ev = ui_events.drain_secret_blocks()
assert "5 personal data item(s) found by Amazon Bedrock in 2 file(s)" in out and "db/seed.sql:1501: EMAIL" in out, out
assert [e["kind"] for e in ev] == ["personal data found"], ev
facts = {"tests": [], "packs": [], "parity": [], "config": [], "root": repo}
section = agent._verification_section(facts, [])
assert "### Personal data (Amazon Bedrock scan)" in section and "`db/seed.sql:3002` US_SOCIAL_SECURITY_NUMBER" in section, section
blob = out + json.dumps(ev) + section + json.dumps([str(f) for f in r["findings"]])
leaks = [t for t, v in VALUES.items() if v in blob]
assert not leaks, leaks
print("5 ok: tool result, chat event and PR section list type + file:line, and no value appears anywhere")
print("ALL OK")
