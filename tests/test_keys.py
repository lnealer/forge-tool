"""Secret patterns in Java, scrub flow, public-key decision, commit gate.

Offline: no AWS calls (Bedrock and GitHub are stubbed), git runs against local
bare repositories in a temp dir. Run directly or through tests/run_all.py.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON_DIR = os.path.join(os.path.dirname(HERE), "python")
sys.path.insert(0, PYTHON_DIR)
os.chdir(PYTHON_DIR)
import os, subprocess, sys, tempfile, json
import workdir, ui_events, guardrails, settings, techstack

# Planted values are assembled at runtime so no secret-shaped literal sits in this
# source file (forge's own scanner and GitHub push protection would flag it).
BEGIN_PRIV = "-----BEGIN " + "PRIVATE KEY-----"
BEGIN_PUB = "-----BEGIN " + "PUBLIC KEY-----"
NEW_SKS = "new Secret" + "KeySpec"

def sh(args, cwd): return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout
def w(root, rel, text):
    path = os.path.join(root, rel); os.makedirs(os.path.dirname(path), exist_ok=True); open(path, "w").write(text)

# ---- fixture: a module with planted keys, pushed to a local bare origin ----
tmp = tempfile.mkdtemp(); workdir.set_working_dir(tmp)
branch = settings.GIT_SOURCE_BRANCH or "main"
remote = os.path.join(tmp, "remote.git"); sh(["git", "init", "-q", "--bare", "-b", branch, remote], tmp)
repo = os.path.join(tmp, "repo"); os.makedirs(repo); sh(["git", "init", "-q", "-b", branch], repo)
sh(["git", "config", "user.name", "t"], repo); sh(["git", "config", "user.email", "t@t"], repo)
PLANTED = {
    "AES_HEX": "00112233445566778899aabbccddeeff",
    "SPEC": "MySuperSecretKey",
    "GENERIC": "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MA==",
    "PUB_B64": "MIIBIjAN" + "BgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gunVTLw7on",
}
w(repo, "pom.xml", "<project><artifactId>demo</artifactId><packaging>jar</packaging></project>")
crypto = f'''package demo;

import javax.crypto.spec.SecretKeySpec;

public final class CryptoUtil {{
    private static final String AES_KEY = "{PLANTED["AES_HEX"]}";
    private static final String KEY = "{PLANTED["GENERIC"]}";
    private static final byte[] IV = new byte[]{{0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b, 0x0c}};
    private static final String SIGNING_PEM = "{BEGIN_PRIV}\\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\\n-----END PRIVATE KEY-----";
    private static final String VERIFY_PEM = "{BEGIN_PUB}\\nMIIBIjANBgkq\\n-----END PUBLIC KEY-----";
    public static final String PUBLIC_KEY = "{PLANTED["PUB_B64"]}";
    public static final String MESSAGE_KEY = "error.validation.required";

    static SecretKeySpec spec() {{
        return {NEW_SKS}("{PLANTED["SPEC"]}".getBytes(), "AES");
    }}
}}
'''
w(repo, "src/main/java/demo/CryptoUtil.java", crypto)
w(repo, "src/main/java/demo/Clean.java", "package demo;\nclass Clean { String k = System.getenv(\"AES_KEY\"); }\n")
sh(["git", "add", "-A"], repo); sh(["git", "commit", "-qm", "init"], repo)
sh(["git", "remote", "add", "origin", remote], repo); sh(["git", "push", "-q", "origin", branch], repo); sh(["git", "fetch", "-q", "origin"], repo)

import agent, git_utils
def no_values(text, where):
    for name, value in PLANTED.items():
        if name != "PUB_B64":  # public keys may be shown redacted too; check secrets strictly
            assert value not in text, f"{name} value leaked in {where}"
    assert PLANTED["PUB_B64"] not in text, f"public key value leaked in {where}"

# 1. scanner severities
f = guardrails.scan_text(crypto, "CryptoUtil.java")
kinds = {(x.line, x.kind, x.severity) for x in f}
lines = {x.line: x.severity for x in f}
assert lines == {6: "secret", 7: "secret", 8: "secret", 9: "secret", 10: "public", 11: "public", 15: "secret"}, sorted(kinds)
print("1 ok: 5 secrets + 2 public keys at the right lines; MESSAGE_KEY decoy clean")

# 2. scan_for_secrets: two sections, two events, STOP line
ui_events.drain_secret_blocks()
out = agent.scan_for_secrets.invoke({"path": "repo"})
ev = ui_events.drain_secret_blocks()
assert "5 hardcoded secret(s)" in out and "2 embedded public key(s)" in out and "STOP now" in out, out
assert [e["kind"] for e in ev] == ["found in repository", "public key found"], ev
no_values(out, "scan_for_secrets result"); no_values(json.dumps(ev), "UI events")
print("2 ok: secrets and public keys reported separately, STOP for the user's decision, no values")

# 3. detection
stack = json.loads(agent.detect_tech_stack.invoke({"repo_dir": "repo"}))
app = {p["id"]: p for p in stack["packs"]["applicable"]}
assert "externalize-secrets" in app and "externalize-public-keys" in app, list(app)
assert app["externalize-secrets"]["evidence"] == ["5 hardcoded secret(s) in 1 file(s)"], app["externalize-secrets"]
assert app["externalize-public-keys"]["evidence"] == ["2 public key(s) in 1 file(s)"]
print("3 ok: both packs applicable with value-free evidence")

# 4. inventory
inv = agent.list_migration_files.invoke({"repo_dir": "repo", "pack_ids": "externalize-secrets,externalize-public-keys"})
assert "CryptoUtil.java  <- externalize-secrets, externalize-public-keys" in inv and "Clean.java" not in inv.split("VERIFY ONLY")[0], inv
print("4 ok: CryptoUtil.java MUST CHANGE for both packs; Clean.java not listed")

# 5. write gate: secret refused, public allowed, env version allowed
writer = agent.make_guarded_write_tool(tmp)
r1 = writer.invoke({"file_path": "repo/src/main/java/demo/Leak.java", "text": f'class Leak {{ String AES_KEY = "{PLANTED["AES_HEX"]}"; }}'})
r2 = writer.invoke({"file_path": "repo/src/main/java/demo/Pub.java", "text": f'class Pub {{ String PUBLIC_KEY = "{PLANTED["PUB_B64"]}"; }}'})
assert r1.startswith("ERROR: write refused") and not r2.startswith("ERROR"), (r1, r2)
os.remove(os.path.join(repo, "src/main/java/demo/Pub.java"))
no_values(r1, "write refusal")
print("5 ok: literal AES key refused; public key alone not blocked")

# 6. commit gate blocks a literal added by the migration
w(repo, "src/main/java/demo/Extra.java", f'class Extra {{ byte[] key = {NEW_SKS}("{PLANTED["SPEC"]}2".getBytes(), "AES").getEncoded(); }}')
blocked = git_utils.create_branch.invoke({"repo_file_path": "repo", "commit_message": "leak"})
assert "commit blocked" in blocked and sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo).strip() == branch, blocked
os.remove(os.path.join(repo, "src/main/java/demo/Extra.java"))
print("6 ok: commit gate blocks a SecretKeySpec literal, no branch created")

# 7. acceptance before scrubbing
acc = agent.check_pack_acceptance.invoke({"repo_dir": "repo", "pack_ids": "externalize-secrets,externalize-public-keys"})
assert "externalize-secrets: 5 leftover(s)" in acc and "externalize-public-keys: 2 leftover(s)" in acc, acc
assert "CryptoUtil.java:6: AES/symmetric key assignment (value redacted)" in acc, acc
no_values(acc, "acceptance")
print("7 ok: acceptance lists 5 secret + 2 public leftovers as file:line, values redacted")

# 8. scrub secrets the way the pack prescribes -> externalize-secrets CLEAN, commit passes
scrubbed = crypto
scrubbed = scrubbed.replace(f'"{PLANTED["AES_HEX"]}"', 'requiredEnv("AES_KEY")')
scrubbed = scrubbed.replace(f'"{PLANTED["GENERIC"]}"', 'requiredEnv("ENCRYPTION_KEY")')
scrubbed = scrubbed.replace("new byte[]{0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b, 0x0c}", 'java.util.HexFormat.of().parseHex(requiredEnv("AES_IV"))')
scrubbed = scrubbed.replace('"' + BEGIN_PRIV + '\\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\\n-----END PRIVATE KEY-----"', 'requiredEnv("SIGNING_KEY_PEM")')
scrubbed = scrubbed.replace(f'"{PLANTED["SPEC"]}".getBytes()', 'requiredEnv("AES_SPEC_KEY").getBytes(java.nio.charset.StandardCharsets.UTF_8)')
scrubbed = scrubbed.replace("    static SecretKeySpec spec()", '    private static String requiredEnv(String n) { String v = System.getenv(n); if (v == null) throw new IllegalStateException(n); return v; }\n\n    static SecretKeySpec spec()')
r3 = writer.invoke({"file_path": "repo/src/main/java/demo/CryptoUtil.java", "text": scrubbed})
assert not r3.startswith("ERROR"), r3
acc2 = agent.check_pack_acceptance.invoke({"repo_dir": "repo", "pack_ids": "externalize-secrets,externalize-public-keys"})
assert "externalize-secrets: CLEAN" in acc2 and "externalize-public-keys: 2 leftover(s)" in acc2, acc2
ok = git_utils.create_branch.invoke({"repo_file_path": "repo", "commit_message": "Externalize hardcoded secrets"})
assert ok.startswith("Pushed branch 'forge-upgrade-"), ok
pushed = sh(["git", "show", "HEAD:src/main/java/demo/CryptoUtil.java"], repo)
for name in ("AES_HEX", "SPEC", "GENERIC"):
    assert PLANTED[name] not in pushed, name
print("8 ok: after scrubbing, externalize-secrets CLEAN, commit pushed, no secret value in the pushed file; public keys still pending")

# 9. scrub public keys too -> CLEAN
scrubbed2 = scrubbed.replace(f'"{PLANTED["PUB_B64"]}"', 'requiredEnv("JWT_PUBLIC_KEY")').replace('"' + BEGIN_PUB + '\\nMIIBIjANBgkq\\n-----END PUBLIC KEY-----"', 'requiredEnv("VERIFY_PUBLIC_KEY_PEM")')
writer.invoke({"file_path": "repo/src/main/java/demo/CryptoUtil.java", "text": scrubbed2})
acc3 = agent.check_pack_acceptance.invoke({"repo_dir": "repo", "pack_ids": "externalize-public-keys"})
assert "externalize-public-keys: CLEAN" in acc3, acc3
print("9 ok: after scrubbing public keys, externalize-public-keys CLEAN")

# 10. redaction of chat output
red = guardrails.redact(crypto)
no_values(red, "redact()")
print("10 ok: redact() removes every planted value from chat text")

# 11. chat buttons queue the right messages
os.environ.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
import chat
sent = []
chat._queue_turn = lambda m: sent.append(m)
chat._public_key_decision("scrub"); chat._public_key_decision("keep")
assert "add the pack externalize-public-keys" in sent[0] and "do not add externalize-public-keys" in sent[1], sent
print("11 ok: Scrub / Continue buttons queue the right instructions")
print("ALL OK")
