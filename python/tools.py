from langchain_core.tools import tool
import subprocess
from datetime import datetime
from langchain_aws.retrievers import AmazonKnowledgeBasesRetriever
from langchain_core.tools import create_retriever_tool
import os
from utils import get_logger
import json
import techstack
import ui_events

logger = get_logger()

def load_kb_tool():
    kb_id = os.getenv("KNOWLEDGE_BASE_ID") # could also be an ssm param?
    retriever = AmazonKnowledgeBasesRetriever(
        knowledge_base_id=kb_id, 
            region_name="us-east-1",
        retrieval_config={"managedSearchConfiguration": {"numberOfResults": 4}},
    )

    kb_tool = create_retriever_tool(
        retriever,
        name="code_upgrade_knowledge_base",
        description="Searches for coding guidelines for company-specific standards."
    )

    return kb_tool
 
@tool
def run_maven_test(code_dir: str) -> str:
    """Runs a shell command using subprocess to run maven tests and returns output.
    Args:
        code_dir: The code directory to execute from.
    """
    logger.info(f"Running: mvn clean test -f {code_dir}")
    command = f'mvn clean test -q -f {code_dir}'
    try:
        # Capture the output and check the return code
        result = subprocess.run(command, check=True, shell=True, capture_output=True, text=True)
        logger.info(f"Mvn output: {result.stdout}")
        return result.stdout if result.returncode == 0 else result.stderr
    except subprocess.CalledProcessError as e:
        logger.info(f"Command '{command}' failed with return code {e.returncode}")
        logger.info(f"Mvn output: {e.stderr} {e.stdout}")
        return e.stdout + e.stderr

@tool
def run_maven_compile(code_dir: str) -> str:
    """Runs a shell command using subprocess to compile application and returns output.
    Args:
        code_dir: The code directory to execute from.
    """
    logger.info(f"Running maven compile {code_dir}")
    command = f'mvn clean package -q -f {code_dir}'
    try:
        # Capture the output and check the return code
        result = subprocess.run(command, check=True, shell=True, capture_output=True, text=True)
        logger.info(f"Mvn output: {result.stdout}")
        return result.stdout if result.returncode == 0 else result.stderr
    except subprocess.CalledProcessError as e:
        logger.info(f"Command '{command}' failed with return code {e.returncode}")
        logger.info(f"Mvn output: " + e.stdout + e.stderr)
        return e.stdout+e.stderr

@tool
def get_current_timestamp():
    """ Get the current date and time """
    return datetime.now()    

@tool
def list_guideline_packs() -> str:
    """List the company's migration guideline packs: which migrations have guidance.

    One line per pack: id, tier, title (from -> to), what it depends on, and
    the decisions it needs (e.g. container=tomcat). detect_tech_stack already
    reports which packs apply to the repo; use the ids in propose_migration_plan
    and query code_upgrade_knowledge_base for a pack's transform guidance.
    """
    packs = techstack.load_packs()
    if not packs:
        return f"No guideline packs found in."
    lines = []
    for pack in packs:
        extra = []
        if pack.get("depends_on"):
            extra.append("after: " + ", ".join(pack["depends_on"]))
        if pack.get("decisions"):
            extra.append("decisions: " + ", ".join(pack["decisions"]))
        if pack.get("status"):
            extra.append(f"status: {pack['status']}")
        lines.append(f"- {pack['id']} [{pack.get('tier', '?')}]: {pack.get('title', '')}" + (f"  ({'; '.join(extra)})" if extra else ""))
    return f"{len(lines)} guideline pack(s):\n" + "\n".join(lines)
_RISKS = ("low", "medium", "high")

@tool
def propose_migration_plan(plan_json: str) -> str:
    """Record the migration plan and show it to the user for approval.

    plan_json is a JSON object:
    {"goal": "<the upgrade goal>", "summary": "<2-4 sentences incl. components already at target>",
     "items": [{"component": "Struts", "current": "2.5.30", "target": "7.x per struts2-modernize",
                "pack": "struts2-modernize", "scope": "ams-internal/* (12 actions, struts.xml)",
                "in_scope": true, "risk": "medium", "notes": "..."}]}
    in_scope=false marks optional candidates the user can opt into. After calling
    this, stop and wait for the user's approval before editing any file.

    Args:
        plan_json: The plan as a JSON string in the shape above.
    """
    logger.info("Proposing migration plan")
    try:
        plan = json.loads(plan_json)
    except (TypeError, ValueError) as e:
        return f"ERROR: plan_json is not valid JSON ({e}). Send a JSON object with goal, summary and items."
    if not isinstance(plan, dict) or not isinstance(plan.get("items"), list) or not plan["items"]:
        return "ERROR: plan must be an object with a non-empty 'items' list."
    items = []
    for i, raw in enumerate(plan["items"]):
        if not isinstance(raw, dict):
            return f"ERROR: item {i} is not an object."
        item = {k: str(raw.get(k, "")).strip() for k in ("component", "current", "target", "pack", "scope", "notes")}
        if not item["component"] or not item["target"]:
            return f"ERROR: item {i} needs at least 'component' and 'target'."
        item["in_scope"] = bool(raw.get("in_scope", True))
        risk = str(raw.get("risk", "medium")).lower()
        item["risk"] = risk if risk in _RISKS else "medium"
        items.append(item)
    plan = {"goal": str(plan.get("goal", "")).strip(), "summary": str(plan.get("summary", "")).strip(), "items": items}
    ui_events.set_migration_plan(plan)
    ui_events.set_proposed_packs([i["pack"] for i in items if i["in_scope"]])
    n_in = sum(1 for i in items if i["in_scope"])
    return (f"Plan recorded and shown to the user ({n_in} in-scope item(s), {len(items) - n_in} optional). "
                "STOP now: give the user a short readable version and wait for their approval before changing any file.")
    # return f"Plan recorded and shown to the user ({n_in} in-scope item(s)). Proceed with the in-scope items."


@tool
def list_migration_files(repo_dir: str = "repo", pack_ids: str = "") -> str:
    """List every file the plan's packs apply to: what must change, in which order, once per file.

    Built from each pack's applies_to selectors (no guessing). A file selected
    by several packs appears once with its packs in dependency order: edit it
    ONCE, applying each listed pack's guidance in that order. MUST CHANGE files
    still contain what a pack removes; VERIFY ONLY files are selected but have
    nothing known to be wrong - leave them unless the guidance names them.

    Args:
        repo_dir: The cloned repo, relative to the working directory (e.g. "repo").
        pack_ids: Optional comma-separated pack ids. Empty = the approved plan's packs
            (or the proposed plan's in-scope packs before approval).
    """
    ids, source = _plan_pack_ids(pack_ids)
    if not ids:
        return "ERROR: no packs given and no plan recorded. Call propose_migration_plan first, or pass pack_ids."
    packs, unknown = techstack.resolve_packs(ids)
    if not packs:
        return f"ERROR: none of these are guideline packs: {', '.join(unknown)}."
    repo = techstack.Repo(repo_dir)
    inventory, notes = techstack.build_inventory(repo, packs)
    order = [p["id"] for p in packs]
    must = {f: e for f, e in inventory.items() if any(x["must_change"] and not x["detect_only"] for x in e)}
    manual = {f: e for f, e in inventory.items() if f not in must and any(x["must_change"] and x["detect_only"] for x in e)}
    verify = {f: e for f, e in inventory.items() if f not in must and f not in manual}

    lines = [f"Migration inventory from the {source}: {len(packs)} pack(s) in dependency order: {', '.join(order)}"]
    if unknown:
        lines.append(f"not guideline packs (ignored): {', '.join(unknown)}")
    lines.append(f"\nMUST CHANGE: {len(must)} file(s). Edit each once, applying its packs left to right.")
    by_module = {}
    for f in sorted(must):
        by_module.setdefault(_module_of(f), []).append(f)
    shown = 0
    for module, files in by_module.items():
        lines.append(f"  [{module}]")
        for f in files:
            if shown >= 300:
                break
            todo = [x["pack"] for x in must[f] if x["must_change"] and not x["detect_only"]]
            rest = [x["pack"] for x in must[f] if x["pack"] not in todo]
            lines.append(f"    {f[len(module) + 1:] if module != '.' and f.startswith(module + '/') else f}  <- {', '.join(todo)}"
                         + (f"  (verify: {', '.join(rest)})" if rest else ""))
            shown += 1
    if len(must) > shown:
        lines.append(f"    ... {len(must) - shown} more; call again with fewer pack_ids")
    if manual:
        lines.append(f"\nMANUAL (detect-only packs, no transform guidance): {len(manual)} file(s)")
        lines += [f"    {f}  <- {', '.join(x['pack'] for x in manual[f])}" for f in sorted(manual)[:40]]
    counts = {}
    for f in verify:
        counts[_module_of(f)] = counts.get(_module_of(f), 0) + 1
    lines.append(f"\nVERIFY ONLY: {len(verify)} file(s) selected with nothing known to be wrong: "
                 + "; ".join(f"{m} {n}" for m, n in sorted(counts.items())))
    if notes:
        lines.append("\nNOT EVALUATED: " + "; ".join(notes))
    lines.append("\nNEXT: migrate the MUST CHANGE files, then run check_pack_acceptance.")
    return "\n".join(lines)

def _module_of(path):
    head, sep, _ = path.partition("/src/")
    return head if sep else (os.path.dirname(path) or ".")

def _plan_pack_ids(pack_ids):
    """Explicit comma-separated ids win; else the approved (or proposed) plan's packs."""
    explicit = [p.strip() for p in (pack_ids or "").split(",") if p.strip()]
    return explicit, "pack ids passed in"

@tool
def check_pack_acceptance(repo_dir: str = "repo", pack_ids: str = "") -> str:
    """Check that each pack's migration is complete, per its acceptance rules.

    Reports every leftover as file:line (a no_match pattern still present), the
    count_unchanged checks against the source branch, and the checks code cannot
    evaluate (build, parity, conditional rules) for the PR. Run it after
    migrating and again before review; a pack is done when it is clean or each
    leftover is explained in the PR.

    Args:
        repo_dir: The cloned repo, relative to the working directory (e.g. "repo").
        pack_ids: Optional comma-separated pack ids. Empty = the approved plan's packs.
    """
    ids, source = _plan_pack_ids(pack_ids)
    if not ids:
        return "ERROR: no packs given and no plan recorded. Call propose_migration_plan first, or pass pack_ids."
    packs, unknown = techstack.resolve_packs(ids)
    repo = techstack.Repo(repo_dir)
    lines, open_packs = [f"Acceptance for the {source}:"], []
    for pack in packs:
        result = techstack.check_acceptance(repo, pack)
        tag = " (detect-only: manual migration)" if pack.get("status") == "detect-only" else ""
        if result["clean"]:
            lines.append(f"\n{pack['id']}{tag}: CLEAN")
        else:
            open_packs.append(pack["id"])
            lines.append(f"\n{pack['id']}{tag}: {result['leftover_count']} leftover(s)")
            lines += [f"    {l}" for l in result["leftovers"]]
            if result["leftover_count"] > len(result["leftovers"]):
                lines.append(f"    ... {result['leftover_count'] - len(result['leftovers'])} more")
        lines += [f"  {c}" for c in result["count_changes"]]
        lines += [f"  manual: {m}" for m in result["manual_checks"]]
    if unknown:
        lines.append(f"\nnot guideline packs (ignored): {', '.join(unknown)}")
    if open_packs:
        lines.append(f"\nACTION: not finished: {', '.join(open_packs)}. Migrate the leftovers (each file once), "
                     "or list each one you leave, with the reason, under 'Detected but not migrated' in the PR.")
    else:
        lines.append("\nAll packs clean. List the manual checks in the PR for the human reviewer.")
    return "\n".join(lines)

