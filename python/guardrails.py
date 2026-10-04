"""Local guardrails: secret detection, output redaction, and the manual-review queue.

Two layers protect against secrets leaking through the agent:

1. This module scans text the agent writes (guarded ``write_file``), the staged
   diff before every commit (so nothing with a key in it gets pushed), and the
   assistant's chat output (redacted before rendering).
2. An Amazon Bedrock Guardrail (infra/cloudformation/guardrail.yaml) applies
   PII and regex filters on the model's input and output server-side.

The manual-review queue is how the reviewer model hands flagged files to the
chat UI: tools may run off the Streamlit script thread, so they append here and
chat.py drains the queue after each agent turn.
"""

import os
import re
import threading
from dataclasses import dataclass, field
from typing import List

import settings

# Patterns are deliberately conservative: each needs either a well-known token
# prefix or an assignment context, so ordinary Java identifiers do not trip them.
#
# Severity: "secret" material (private and symmetric keys, tokens, passwords) is
# blocked at write and commit time and always externalized by the migration.
# "public" material (public keys) is not secret; it is reported and the user
# decides whether to move it to configuration. First match wins per line, so
# secret patterns come first.
_PUBLIC_KEY_B64 = (r"MI[IG][A-Za-z0-9+/]{2}(?:jAN|MA0)BgkqhkiG9w0BAQEFAAO"
                   r"|MIGfMA0GCSqGSIb3DQEBAQUAA4"
                   r"|MFkwEwYHKoZIzj0CAQY")
_BYTE = r"(?:\(byte\)\s*)?-?(?:0x[0-9a-fA-F]{1,2}|\d{1,3})"

SECRET_PATTERNS = [
    (
        "SSH/PEM private key",
        r"-----BEGIN (?:RSA |OPENSSH |EC |DSA |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----",
        "secret",
    ),
    ("AWS access key id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", "secret"),
    (
        "AWS secret access key",
        r"(?i)aws[_\-\s]*secret[_\-\s]*(?:access[_\-\s]*)?key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{40}\b",
        "secret",
    ),
    (
        "GitHub token",
        r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b",
        "secret",
    ),
    (
        "AES/symmetric key assignment",
        r"(?i)\b(?:aes|secret|private|encryption|symmetric|hmac|signing)[_\-\s]*key"
        r"\s*[:=]\s*['\"]?(?:[A-Fa-f0-9]{32,64}|[A-Za-z0-9+/]{22,}={0,2})['\"]?",
        "secret",
    ),
    (
        # new SecretKeySpec("MySuperSecretKey".getBytes(), "AES")
        "SecretKeySpec with a literal key",
        r'new\s+SecretKeySpec\s*\(\s*"[^"]{8,}"',
        "secret",
    ),
    (
        # byte[] key = {0x01, 0x02, ...};  static final byte[] IV = new byte[]{1, 2, ...};
        "Byte-array key literal",
        r"(?i)\b\w*(?:key|secret|iv)\w*\s*(?:\[\s*\])?\s*=\s*(?:new\s+byte\s*\[\s*\]\s*)?\{\s*"
        + _BYTE + r"(?:\s*,\s*" + _BYTE + r"){7,}",
        "secret",
    ),
    (
        # KEY = "Zm9vYmFy..."  ENC_KEY = "0011aabb..." - not PUBLIC_KEY, not a public-key blob,
        # and the value must mix letters and digits (message keys like "error.required" have dots).
        "Key constant with a literal value",
        r"\b(?!\w*(?:PUBLIC|Public|public|PUB_|Pub[A-Z]))\w*(?:KEY|Key|key)\s*=\s*\""
        r"(?!" + _PUBLIC_KEY_B64 + r")"
        r"(?=[A-Za-z0-9+/=]*\d)(?=[A-Za-z0-9+/=]*[A-Za-z])[A-Za-z0-9+/]{24,}={0,2}\"",
        "secret",
    ),
    (
        "Hardcoded password",
        r"(?i)\b(?:password|passwd|pwd)\s*[:=]\s*['\"][^'\"\s]{6,}['\"]",
        "secret",
    ),
    (
        # GATEWAY_PASSWORD = "Qx7!..."; needs a digit or symbol so "j_password" style names stay clean
        "Hardcoded password constant",
        r"(?i)\b\w+_(?:password|passwd|pwd)\s*=\s*\"(?=[^\"]*[\d!@#$%^&*])[^\"\s]{6,}\"",
        "secret",
    ),
    (
        # SETTLEMENT_API_KEY = "...", DB_CLIENT_SECRET = "..."
        "API key / token constant",
        r"(?i)\b\w+_(?:api_?key|api_?secret|access_?token|auth_?token|client_?secret)\s*=\s*\""
        r"(?=[^\"]*\d)[A-Za-z0-9_\-\.+/=]{16,}\"",
        "secret",
    ),
    (
        # Liberty server.xml: <variable name="keystore.password" defaultValue="literal"/>
        "Password variable default",
        r"(?i)<variable\s+name=\"[^\"]*(?:password|secret)[^\"]*\"\s+defaultValue=\"[^\"$]{6,}\"",
        "secret",
    ),
    (
        "API key / token assignment",
        r"(?i)\b(?:api[_\-]?key|api[_\-]?secret|access[_\-]?token|auth[_\-]?token|client[_\-]?secret)"
        r"\s*[:=]\s*['\"][A-Za-z0-9_\-\.]{16,}['\"]",
        "secret",
    ),
    ("Slack token", r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", "secret"),
    ("JWT", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b", "secret"),
    (
        "Private key material in string",
        r"(?i)\bprivate[_\-\s]*key\s*[:=]\s*['\"][A-Za-z0-9+/=]{40,}['\"]",
        "secret",
    ),
    ("PEM public key", r"-----BEGIN (?:RSA )?PUBLIC KEY-----", "public"),
    ("X.509 public key (base64)", r"\b(?:" + _PUBLIC_KEY_B64 + r")[A-Za-z0-9+/=]{40,}", "public"),
]

SEVERITIES = ("secret", "public")

_COMPILED = [(name, re.compile(pattern), severity) for name, pattern, severity in SECRET_PATTERNS]

# Externalized values are the *fix* for a hardcoded secret, so they must never be
# reported as one: ${DB_PASSWORD}, $(VAR), @VAR@, %VAR%, {{ var }}.
_PLACEHOLDER = re.compile(r"\$\{[^}]*\}|\$\([^)]*\)|@[A-Za-z0-9_.]+@|%[A-Za-z0-9_.]+%|\{\{[^}]*\}\}")

_SKIP_DIRS = {".git", ".svn", "node_modules", "target", "build", ".idea", ".venv", "venv"}
_MAX_FILE_BYTES = 1_000_000


def _allowlist():
    """Patterns that suppress a finding, from SECRET_SCAN_ALLOWLIST (';'-separated)."""
    raw = settings.SECRET_SCAN_ALLOWLIST
    return [re.compile(p) for p in raw.split(";") if p.strip()] if raw else []


@dataclass
class Finding:
    kind: str
    path: str
    line: int
    snippet: str  # already redacted
    severity: str = "secret"

    def __str__(self):
        return f"{self.path}:{self.line}: {self.kind} -> {self.snippet}"


def _redact_match(kind, match):
    text = match.group(0)
    # Keep the first few characters so the user can recognise which value it was.
    head = text[:4] if len(text) > 12 else ""
    return f"{head}[REDACTED {kind}]"


def redact(text):
    """Replace every detected secret in *text* with a redaction marker."""
    if not text:
        return text
    for kind, pattern, _severity in _COMPILED:
        text = pattern.sub(lambda m, k=kind: _redact_match(k, m), text)
    return text


def scan_text(text, path="<text>", line_offset=0, severities=SEVERITIES):
    """Return findings for every secret-looking match in *text*.

    *severities* limits what is reported: the write and commit gates pass
    ("secret",) so public keys never block them.
    """
    if not settings.SECRET_SCAN_ENABLED or not text:
        return []
    allow = _allowlist()
    findings = []
    for idx, line in enumerate(text.splitlines(), start=1):
        if any(a.search(line) for a in allow):
            continue
        # Match against a copy with placeholders removed outright (a substitute
        # word would itself look like a quoted value); report the real line, redacted.
        probe = _PLACEHOLDER.sub("", line)
        for kind, pattern, severity in _COMPILED:
            if pattern.search(probe):
                if severity in severities:
                    findings.append(
                        Finding(kind, path, line_offset + idx, redact(line.strip())[:160], severity)
                    )
                break  # one finding per line is enough; secret patterns are checked first
    return findings


def _looks_binary(path):
    try:
        if os.path.getsize(path) > _MAX_FILE_BYTES:
            return True
        with open(path, "rb") as handle:
            return b"\0" in handle.read(4096)
    except OSError:
        return True


def scan_tree(root, severities=SEVERITIES):
    """Scan every text file under *root*, skipping VCS and build directories."""
    findings = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            path = os.path.join(dirpath, name)
            if _looks_binary(path):
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    content = handle.read()
            except OSError:
                continue
            findings.extend(scan_text(content, os.path.relpath(path, root), severities=severities))
    return findings


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def scan_diff(diff_text, severities=("secret",)):
    """Scan only the added lines of a unified diff (``git diff -U0`` output).

    Pre-existing secrets in the repo are reported by scan_tree; this is the
    gate that stops the agent from *introducing* or moving one.
    """
    findings = []
    current_file = "<diff>"
    line_no = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            current_file = raw[4:].strip()
            current_file = current_file[2:] if current_file.startswith("b/") else current_file
            continue
        if raw.startswith("--- ") or raw.startswith("diff ") or raw.startswith("index "):
            continue
        hunk = _HUNK.match(raw)
        if hunk:
            line_no = int(hunk.group(1))
            continue
        if raw.startswith("+"):
            findings.extend(scan_text(raw[1:], current_file, line_offset=line_no - 1, severities=severities))
            line_no += 1
        elif not raw.startswith("-"):
            line_no += 1
    return findings


def format_findings(findings):
    return "\n".join(f"  - {f}" for f in findings)


# --- manual review queue ---------------------------------------------------

_REVIEW_QUEUE: List[dict] = []
_REVIEW_LOCK = threading.Lock()


def enqueue_manual_review(item):
    """Hand a flagged file to the chat UI. *item* is rendered by chat.py."""
    with _REVIEW_LOCK:
        _REVIEW_QUEUE.append(item)


def drain_manual_reviews():
    """Return and clear everything queued since the last drain."""
    with _REVIEW_LOCK:
        items = list(_REVIEW_QUEUE)
        _REVIEW_QUEUE.clear()
    return items
