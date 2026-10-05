"""UI-facing events raised from inside tools, drained by the chat after each step.

Tools may run off the Streamlit script thread and must never call st.*; they
push here and chat.py renders. (The reviewer's manual-review queue lives in
guardrails.py; this module holds the secret-guardrail events.)
"""

import threading

_LOCK = threading.Lock()
_SECRET_BLOCKS = []


def push_secret_block(kind, path, findings):
    """Record that the secret guardrail acted. *findings* are already-redacted strings."""
    with _LOCK:
        _SECRET_BLOCKS.append({"kind": kind, "path": path, "findings": [str(f) for f in findings]})


_MIGRATION_PLAN = None


def set_migration_plan(plan):
    """Replace the current proposed plan (the UI renders it with an Approve button)."""
    global _MIGRATION_PLAN
    with _LOCK:
        _MIGRATION_PLAN = plan


def take_migration_plan():
    global _MIGRATION_PLAN
    with _LOCK:
        plan, _MIGRATION_PLAN = _MIGRATION_PLAN, None
    return plan


# Pack ids of the plan. Proposed = the in-scope packs of the last proposed plan;
# approved = frozen when the user approves it. The file inventory and the
# acceptance checks use the approved set, never a fresh detection: once a
# migration is partly done a pack's detect rules can stop matching although
# its work is unfinished.
_PROPOSED_PACKS = []
_APPROVED_PACKS = []


def set_proposed_packs(pack_ids):
    global _PROPOSED_PACKS
    with _LOCK:
        _PROPOSED_PACKS = list(dict.fromkeys(p for p in pack_ids if p))


def approve_packs(pack_ids):
    """Freeze the pack set at plan approval (called by the chat's Approve buttons)."""
    global _APPROVED_PACKS
    with _LOCK:
        _APPROVED_PACKS = list(dict.fromkeys(p for p in pack_ids if p))


def plan_packs():
    """(pack ids, source): the approved set if there is one, else the proposed in-scope set."""
    with _LOCK:
        if _APPROVED_PACKS:
            return list(_APPROVED_PACKS), "approved plan"
        return list(_PROPOSED_PACKS), "proposed plan (not approved yet)"


# Files the user approved as-is in the manual-review panel. The PR gate and the
# test-parity check accept these; everything else must pass on its own.
_APPROVED_FILES = set()


def approve_file(path):
    with _LOCK:
        _APPROVED_FILES.add(path)


def approved_files():
    with _LOCK:
        return set(_APPROVED_FILES)


# Packs the USER accepted as not migrated (gate refusal panel). The model can
# never waive an approved pack; only these are allowed through unclean.
_WAIVED_PACKS = set()


def waive_pack(pack_id):
    with _LOCK:
        _WAIVED_PACKS.add(pack_id)


def waived_packs():
    with _LOCK:
        return set(_WAIVED_PACKS)


# Refusals of create_pull_request with unfinished packs, for the chat's decision panel.
_GATE_REFUSALS = []


def push_gate_refusal(unfinished, failures):
    """*unfinished*: [(pack id, leftover count)]; *failures*: every open item (text)."""
    with _LOCK:
        _GATE_REFUSALS.append({"unfinished": list(unfinished), "failures": list(failures)})


def drain_gate_refusals():
    with _LOCK:
        items = list(_GATE_REFUSALS)
        _GATE_REFUSALS.clear()
    return items


def drain_secret_blocks():
    with _LOCK:
        items = list(_SECRET_BLOCKS)
        _SECRET_BLOCKS.clear()
    return items
