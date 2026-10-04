"""Find personal data in the cloned repository with Amazon Bedrock (report only).

The local scanner (guardrails.py) finds secrets by pattern; it does not know what
a name, an address or a phone number looks like. This module sends the
repository's data-bearing files (fixtures, SQL seeds, properties, test sources)
through a dedicated Bedrock Guardrail with the ApplyGuardrail API and reports
the personal data it detects by type and file:line.

Values are never kept: each detected match is located in the text to find its
line number and then discarded. The scan guardrail never sits on model traffic
(see infra/terraform/modules/guardrail), and nothing in the repository is
changed - the findings go to the chat and the pull request.
"""

import hashlib
import os
import threading

import boto3
from botocore.config import Config

import guardrails
import settings
import techstack
from utils import get_logger

logger = get_logger()

_LOCK = threading.Lock()
_CACHE = {}          # sha1(chunk) -> [(type, match)]: identical chunks are not paid for twice
LAST_SCAN = {}       # realpath(root) -> {"findings": [...], "scanned_chars": n, "truncated": bool, "error": str}
_MAX_FILE_BYTES = 1_000_000


def guardrail_config():
    """(id, version) of the PII scan guardrail: .env first, then SSM; (None, None) if absent."""
    gid, version = settings.PII_SCAN_GUARDRAIL_ID, settings.PII_SCAN_GUARDRAIL_VERSION
    if gid:
        return gid, version or "DRAFT"
    try:
        ssm = boto3.client("ssm", region_name=settings.AWS_REGION)
        names = [f"{settings.PARAMETER_STORE_PREFIX}pii_scan_guardrail_id",
                 f"{settings.PARAMETER_STORE_PREFIX}pii_scan_guardrail_version"]
        values = {p["Name"]: p["Value"] for p in ssm.get_parameters(Names=names).get("Parameters", [])}
        gid = values.get(names[0])
        return (gid, values.get(names[1], "DRAFT")) if gid else (None, None)
    except Exception as e:
        logger.warning(f"PII scan guardrail not resolvable ({e})")
        return None, None


_client = None


def _bedrock():
    global _client
    if _client is None:
        _client = boto3.client(
            "bedrock-runtime", region_name=settings.AWS_REGION,
            config=Config(retries={"mode": "adaptive", "max_attempts": 8}, read_timeout=60),
        )
    return _client


def _files(root):
    """Data-bearing text files selected by PII_SCAN_GLOBS (build and VCS dirs excluded)."""
    repo = techstack.Repo(root)
    picked = []
    for pattern in settings.PII_SCAN_GLOBS:
        for rel in repo.glob(techstack._anchor_glob(pattern)):
            if any(part in guardrails._SKIP_DIRS for part in rel.split("/")):
                continue
            full = os.path.join(root, rel)
            try:
                if os.path.getsize(full) > _MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            sample = repo.read(rel, 4096)
            if sample and not techstack._looks_binary(sample):
                picked.append(rel)
    return sorted(set(picked)), repo


def _chunks(text, size):
    """(start_line, chunk_text) pieces of at most *size* characters, split on line boundaries."""
    lines = text.splitlines(keepends=True)
    start, buf, length = 1, [], 0
    for number, line in enumerate(lines, 1):
        if buf and length + len(line) > size:
            yield start, "".join(buf)
            start, buf, length = number, [], 0
        # A single line longer than the chunk size is sent on its own (truncated).
        buf.append(line[:size])
        length += len(buf[-1])
    if buf:
        yield start, "".join(buf)


def _detect(chunk, gid, version):
    """[(type, match)] Bedrock found in *chunk* (cached by content hash)."""
    key = hashlib.sha1(chunk.encode("utf-8", "replace")).hexdigest()
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key], 0
    response = _bedrock().apply_guardrail(
        guardrailIdentifier=gid, guardrailVersion=version, source="OUTPUT",
        content=[{"text": {"text": chunk}}],
    )
    found = []
    for assessment in response.get("assessments", []):
        for entity in assessment.get("sensitiveInformationPolicy", {}).get("piiEntities", []):
            if entity.get("detected", True) and entity.get("match"):
                found.append((entity.get("type", "PII"), entity["match"]))
    units = response.get("usage", {}).get("sensitiveInformationPolicyUnits", 0)
    with _LOCK:
        _CACHE[key] = found
    return found, units


def _lines_of(chunk, start_line, match):
    """Line numbers (in the file) where *match* occurs in *chunk*."""
    numbers, pos = [], chunk.find(match)
    while pos != -1:
        numbers.append(start_line + chunk.count("\n", 0, pos))
        pos = chunk.find(match, pos + max(1, len(match)))
    return numbers or [start_line]


def scan_tree(root):
    """Scan *root* with the Bedrock PII guardrail. Returns the LAST_SCAN entry for it.

    {"findings": [guardrails.Finding(severity="pii")], "files": n, "scanned_chars": n,
     "text_units": n, "truncated": bool, "error": ""}
    """
    root = os.path.realpath(root)
    result = {"findings": [], "files": 0, "scanned_chars": 0, "text_units": 0, "truncated": False, "error": ""}
    if not settings.PII_SCAN_ENABLED:
        result["error"] = "disabled (PII_SCAN_ENABLED=false)"
        return result
    gid, version = guardrail_config()
    if not gid:
        result["error"] = "no PII scan guardrail configured (run ./infra/deploy.sh --no-kb)"
        return result
    files, repo = _files(root)
    result["files"] = len(files)
    seen = set()
    try:
        for rel in files:
            text = repo.read(rel, _MAX_FILE_BYTES)
            for start, chunk in _chunks(text, settings.PII_SCAN_CHUNK_CHARS):
                if result["scanned_chars"] + len(chunk) > settings.PII_SCAN_MAX_CHARS:
                    result["truncated"] = True
                    break
                result["scanned_chars"] += len(chunk)
                found, units = _detect(chunk, gid, version)
                result["text_units"] += units
                for kind, match in found:
                    for line in _lines_of(chunk, start, match):
                        if (rel, line, kind) in seen:
                            continue
                        seen.add((rel, line, kind))
                        result["findings"].append(guardrails.Finding(
                            f"Personal data: {kind}", rel, line, f"[REDACTED {kind}]", "pii"))
            if result["truncated"]:
                break
    except Exception as e:  # the Bedrock scan is an aid; never fail the caller
        logger.warning(f"Bedrock PII scan failed: {e}")
        result["error"] = f"Bedrock PII scan unavailable: {type(e).__name__}: {str(e)[:200]}"
    result["findings"].sort(key=lambda f: (f.path, f.line, f.kind))
    LAST_SCAN[root] = result
    logger.info(f"Bedrock PII scan: {len(result['findings'])} finding(s) in {result['files']} file(s), "
                f"{result['scanned_chars']} chars, {result['text_units']} text unit(s)")
    return result


def summary_by_type(findings):
    counts = {}
    for f in findings:
        kind = f.kind.split(": ", 1)[-1]
        counts[kind] = counts.get(kind, 0) + 1
    return ", ".join(f"{k} {n}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
