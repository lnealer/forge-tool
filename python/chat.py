import json
import traceback

import streamlit as st
from botocore.exceptions import ClientError
from langgraph.errors import GraphRecursionError
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import os

import guardrails
import settings
import techstack
import ui_events
import workdir
from config_upgrade_code import setup_upgrade_code
from utils import get_logger

st.set_page_config(
    page_title=settings.APP_PAGE_TITLE,
    page_icon=settings.APP_PAGE_ICON,
    layout="centered",
)
st.title(f"{settings.APP_PAGE_ICON} {settings.APP_PAGE_TITLE}")
st.caption(settings.APP_CAPTION)

logger = get_logger()


def _as_text(content):
    """Flatten a message content (str or content blocks) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content)


def _one_line(text, limit=160):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- streaming a turn --------------------------------------------------------

def stream_turn(agent, messages, on_tool_call=None, on_tool_result=None, on_text=None, on_step=None):
    """Run one agent turn, reporting each tool call and result as it completes.

    agent.stream() yields the full message list after every graph step; the new
    messages since the previous step are what just happened. Returns the final
    message list.
    """
    seen = len(messages)
    final = messages
    try:
        for state in agent.stream(messages):
            msgs = state["messages"]
            final = _absorb(msgs, seen, on_tool_call, on_tool_result, on_text)
            seen = len(msgs)
            if on_step:
                on_step()
    except GraphRecursionError as e:
        # The turn ran out of steps mid-way. The working copy keeps everything
        # done so far; hand the partial transcript to the caller to resume from.
        raise StepLimitReached(final) from e
    return final


class StepLimitReached(Exception):
    def __init__(self, messages):
        super().__init__("step limit reached")
        self.messages = messages


def _absorb(msgs, seen, on_tool_call, on_tool_result, on_text):
    """Report the messages added since *seen* (tool calls, results, text) and return the list."""
    for message in msgs[seen:]:
        if isinstance(message, AIMessage):
            text = _as_text(message.content).strip()
            if text and on_text:
                on_text(guardrails.redact(text))
            for call in message.tool_calls or []:
                args = _one_line(json.dumps(call.get("args", {}), default=str))
                logger.info(f"→ {call['name']} {args}")
                if on_tool_call:
                    on_tool_call(call["name"], guardrails.redact(args))
        elif isinstance(message, ToolMessage):
            preview = _one_line(guardrails.redact(_as_text(message.content)))
            logger.info(f"← {message.name}: {preview}")
            if on_tool_result:
                on_tool_result(message.name, preview)
    return msgs


# --- migration plan ------------------------------------------------------------

def _collect_migration_plan():
    """Take a plan the agent proposed via propose_migration_plan into the session."""
    plan = ui_events.take_migration_plan()
    if plan:
        plan["status"] = "proposed"
        st.session_state.migration_plan = plan


def _plan_rows(items):
    return [{
        "In scope": "✅ yes" if i.get("in_scope", True) else "optional",
        "Component": i.get("component", ""),
        "Current": i.get("current", ""),
        "Target": i.get("target", ""),
        "Pack": i.get("pack", ""),
        "Scope": i.get("scope", ""),
        "Risk": i.get("risk", ""),
    } for i in items]


def render_migration_plan():
    plan = st.session_state.get("migration_plan")
    if not plan:
        return
    items = plan.get("items", [])
    goal = plan.get("goal") or settings.DEFAULT_UPGRADE_DETAILS
    if plan.get("status") == "approved":
        with st.expander(f"✅ Approved migration plan — {goal}", expanded=False):
            st.dataframe(_plan_rows(items), width="stretch", hide_index=True)
        return
    n_in = sum(1 for i in items if i.get("in_scope", True))
    n_opt = len(items) - n_in
    with st.container(border=True):
        st.markdown(f"#### 🧭 Proposed migration plan — {goal}")
        if plan.get("summary"):
            st.markdown(plan["summary"])
        st.dataframe(_plan_rows(items), width="stretch", hide_index=True)
        notes = [f"- **{i['component']}**: {i['notes']}" for i in items if i.get("notes")]
        if notes:
            st.markdown("\n".join(notes))
        approve_col, all_col = st.columns(2)
        if approve_col.button(f"✅ Approve plan ({n_in} item(s))", key="approve-plan", width="stretch",
                              help="Migrate the in-scope items exactly as proposed."):
            plan["status"] = "approved"
            ui_events.approve_packs([i.get("pack", "") for i in items if i.get("in_scope", True)])
            _queue_turn(
                "Plan approved. Migrate the in-scope items exactly as proposed (optional items stay out), "
                "then run the tests and the review."
            )
        if n_opt and all_col.button(f"➕ Approve incl. {n_opt} optional item(s)", key="approve-plan-all",
                                    width="stretch", help="Also migrate the optional candidates."):
            for i in items:
                i["in_scope"] = True
            plan["status"] = "approved"
            ui_events.approve_packs([i.get("pack", "") for i in items])
            _queue_turn(
                "Plan approved including the optional items: migrate every item in the plan, "
                "then run the tests and the review."
            )
        st.caption("To change the plan, describe the change in the chat and the agent will re-propose it.")


# --- secret guardrail events --------------------------------------------------

def _collect_secret_blocks():
    """Pull events raised by the guardrail inside tools into the session; return the new ones."""
    st.session_state.setdefault("secret_blocks", [])
    new = ui_events.drain_secret_blocks()
    st.session_state.secret_blocks.extend(new)
    return new


_NEXT_STEPS = {
    "found in repository": (
        "*What happens next:* always externalized. The plan includes **externalize-secrets**: each "
        "literal becomes an environment lookup (e.g. `System.getenv(\"AES_KEY\")`, `${DB_PASSWORD}`), "
        "every variable is listed under *Configuration required* in the PR, and the acceptance check "
        "fails until none is left. Values are never copied. Rotate the originals."
    ),
    "personal data found": (
        "*Found by Amazon Bedrock; values never shown.* Reported here and in the PR's verification "
        "section. Nothing is changed: if this is real people's data, ask the agent to replace it with "
        "synthetic data."
    ),
    "public key found": (
        "*Public keys are not secret.* Choose below: **Scrub** moves them to configuration "
        "(pack externalize-public-keys); **Continue** keeps them and lists them in the PR."
    ),
    "write refused": "*What happens next:* the agent is told to read the value from the environment instead.",
    "commit blocked": "*What happens next:* the changes were unstaged; the agent must replace the value with a placeholder before it can push.",
}


def _secret_block_markdown(event):
    lines = "\n".join(f"- {guardrails.redact(f)}" for f in event["findings"])
    tail = _NEXT_STEPS.get(event.get("kind", ""), "")
    return f"🔒 **Secret guardrail — {event['kind']}:** `{event['path']}`\n{lines}" + (f"\n\n{tail}" if tail else "")


def _collect_gate_refusals():
    """Keep only the latest PR-gate refusal with unfinished packs for the decision panel."""
    items = ui_events.drain_gate_refusals()
    if items:
        st.session_state.gate_refusal = items[-1]


def _finish_packs(pack_ids):
    st.session_state.gate_refusal = None
    _queue_turn(f"Finish the approved pack(s) {', '.join(pack_ids)}: migrate the remaining files from "
                "next_migration_files until it is empty, run check_pack_acceptance, re-run the tests on every "
                "reactor, then call create_pull_request again.")


def _accept_pack(pack_id):
    ui_events.waive_pack(pack_id)
    refusal = st.session_state.get("gate_refusal") or {}
    refusal["unfinished"] = [u for u in refusal.get("unfinished", []) if u[0] != pack_id]
    st.session_state.gate_refusal = refusal if refusal.get("unfinished") else None
    _queue_turn(f"The user accepts {pack_id} as not migrated. List its leftovers with the reason under "
                "'Detected but not migrated' in the PR description, then call create_pull_request again "
                "(once the other open items are fixed).")


def render_gate_refusal():
    refusal = st.session_state.get("gate_refusal")
    if not refusal or not refusal.get("unfinished"):
        return
    with st.container(border=True):
        st.markdown("#### 🚧 Pull request blocked: approved work is unfinished")
        st.markdown("\n".join(f"- {f}" for f in refusal["failures"]))
        st.caption("The agent may not descope approved packs. Finish them, or accept specific leftovers yourself.")
        for pack_id, count in refusal["unfinished"]:
            info_col, finish_col, accept_col = st.columns([2, 1, 1])
            info_col.markdown(f"**{pack_id}** - {count} leftover(s)")
            if finish_col.button("▶ Finish", key=f"finish-{pack_id}", width="stretch"):
                _finish_packs([pack_id])
            if accept_col.button("Accept as not migrated", key=f"waive-{pack_id}", width="stretch"):
                _accept_pack(pack_id)
        if len(refusal["unfinished"]) > 1 and st.button("▶ Finish all", key="finish-all-packs", width="stretch"):
            _finish_packs([p for p, _ in refusal["unfinished"]])


def render_secret_blocks():
    items = st.session_state.get("secret_blocks", [])
    if not items:
        return
    with st.container(border=True):
        st.markdown(f"#### 🔒 Secret guardrail: {len(items)} event(s)")
        st.caption(
            "Values are redacted. Writes with secret material are refused, commits that "
            "add one are blocked, and secrets already in the repository are reported — "
            "move them to environment variables or a secrets manager."
        )
        for event in items:
            if event.get("kind") in ("public key found", "personal data found"):
                st.warning(_secret_block_markdown(event))
            else:
                st.error(_secret_block_markdown(event))
        if any(e.get("kind") == "public key found" for e in items):
            render_public_key_decision()
        if st.button("Dismiss", key="dismiss-secret-blocks"):
            st.session_state.secret_blocks = []
            st.rerun()


_PUBLIC_KEY_MESSAGES = {
    "scrub": ("Scrub the public keys: add the pack externalize-public-keys to the migration plan as "
              "in scope, then continue with the test baseline and discovery."),
    "keep": ("Keep the public keys as they are: do not add externalize-public-keys to the plan; list "
             "them in the PR under 'Detected but not migrated'. Continue with the test baseline and discovery."),
}


def _public_key_decision(decision):
    """Record the user's choice for embedded public keys and send it to the agent."""
    st.session_state.public_key_decision = decision
    _queue_turn(_PUBLIC_KEY_MESSAGES[decision])


def render_public_key_decision():
    decision = st.session_state.get("public_key_decision")
    if decision:
        st.caption(f"Public keys: decision made - {'scrub (move to configuration)' if decision == 'scrub' else 'keep as they are'}.")
        return
    scrub_col, keep_col = st.columns(2)
    if scrub_col.button("🧹 Scrub public keys", key="scrub-public-keys", width="stretch",
                        help="Move the embedded public keys to configuration (pack externalize-public-keys)."):
        _public_key_decision("scrub")
    if keep_col.button("▶ Continue (keep them)", key="keep-public-keys", width="stretch",
                       help="Leave the public keys in the code; they are listed in the PR."):
        _public_key_decision("keep")


# --- manual review panel -----------------------------------------------------

def _collect_manual_reviews():
    """Move files the reviewer flagged from the tool-side queue into the session.

    One entry per path (a re-review replaces the older verdict), and files the
    user already approved are not raised again.
    """
    st.session_state.setdefault("manual_reviews", [])
    st.session_state.setdefault("approved_files", set())
    for item in guardrails.drain_manual_reviews():
        if item["path"] in st.session_state.approved_files:
            continue
        st.session_state.manual_reviews = [
            existing for existing in st.session_state.manual_reviews if existing["path"] != item["path"]
        ]
        st.session_state.manual_reviews.append(item)


def _unfinished_packs():
    """[(pack id, leftovers)] for approved packs whose acceptance is not clean.

    Code decides whether the migration is finished, not the model's wording: a
    turn that ends with "still to do" is caught here and offered a Continue.
    Never raises; the chat must render even if the check fails.
    """
    try:
        ids, source = ui_events.plan_packs()
        if source != "approved plan" or not ids:
            return []
        root = workdir.resolve("repo")
        if not os.path.isdir(root):
            return []
        packs, _ = techstack.resolve_packs(ids)
        repo = techstack.Repo(root)
        unfinished = []
        waived = ui_events.waived_packs()
        for pack in packs:
            if pack.get("status") == "detect-only" or pack["id"] in waived:
                continue
            result = techstack.check_acceptance(repo, pack)
            if not result["clean"]:
                unfinished.append((pack["id"], result["leftover_count"]))
        return unfinished
    except Exception as e:
        logger.warning(f"Post-turn acceptance check failed: {e}")
        return []


def _offer_continue_if_unfinished():
    """After a normal turn: if approved packs are unfinished and nothing awaits the user, offer Continue."""
    plan = st.session_state.get("migration_plan") or {}
    if plan.get("status") == "proposed" or st.session_state.get("manual_reviews"):
        return  # the user has a decision to make first
    unfinished = _unfinished_packs()
    if unfinished:
        total = sum(n for _, n in unfinished)
        st.session_state.continue_reason = (
            f"{len(unfinished)} approved pack(s) not finished per their acceptance checks "
            f"({total} leftover(s)): " + ", ".join(f"{p} ({n})" for p, n in unfinished)
        )
        st.session_state.offer_continue = True


def _queue_turn(message):
    """Send *message* to the agent on the next rerun, exactly as if typed in the chat."""
    st.session_state.pending_input = message
    st.rerun()


def _decide(item, decision):
    """Record a per-file decision and turn it into the agent's next instruction."""
    reviews = st.session_state.manual_reviews
    reviews[:] = [r for r in reviews if r["path"] != item["path"]]
    remaining = len(reviews)
    path = item["path"]
    tail = (
        f" {remaining} other flagged file(s) still await my decision; do not push yet."
        if remaining else " All flagged files are now decided."
    )
    if decision == "approve":
        st.session_state.approved_files.add(path)
        ui_events.approve_file(path)
        _queue_turn(f"Approved as-is: `{path}`. Make no further changes to it.{tail}")
    elif decision == "deny":
        _queue_turn(
            f"Rejected: `{path}`. Restore it to the source-branch version with git_restore_file "
            f"and leave it out of this upgrade.{tail}"
        )
    else:  # retry
        issues = "\n".join(
            f"- [{i.get('severity', '?')}] {i.get('description', '')}"
            + (f" ({i['location']})" if i.get("location") else "")
            for i in item.get("issues", [])
        ) or "- (no itemized issues; see the reviewer summary)"
        _queue_turn(
            f"Retry: revise `{path}` (reviewer score {item['score']}/10). Reviewer issues:\n{issues}\n"
            f"Fix the issues that are correct. If an issue contradicts the pack guidance or would not compile "
            f"(for example a package that does not exist, such as jakarta.sql), do NOT apply it: say which issue "
            f"and cite the pack rule. Never change what a test checks.\n"
            f"Then run review_migrated_files for that file again and report the new score.{tail}"
        )


def render_manual_reviews():
    items = st.session_state.get("manual_reviews", [])
    if not items:
        return
    st.warning(
        f"{len(items)} file(s) scored below the review threshold and need your decision "
        "before the pull request is opened."
    )
    for index, item in enumerate(items):
        title = (
            f"{item['path']} — reviewer score {item['score']}/10 "
            f"(threshold {item['threshold']})"
        )
        with st.expander(title, expanded=(index == 0)):
            if item.get("summary"):
                st.markdown(item["summary"])
            for issue in item.get("issues", []):
                location = f" ({issue['location']})" if issue.get("location") else ""
                st.markdown(
                    f"- **{issue.get('severity', '?')}** {issue.get('description', '')}{location}"
                )
            if item.get("error"):
                st.error(item["error"])
            st.code(guardrails.redact(item.get("diff", "")), language="diff")

            approve_col, deny_col, retry_col = st.columns(3)
            key = f"{index}-{item['path']}"
            if approve_col.button("✅ Approve", key=f"approve-{key}", width="stretch",
                                  help="Keep this file exactly as the agent wrote it."):
                _decide(item, "approve")
            if deny_col.button("❌ Deny", key=f"deny-{key}", width="stretch",
                               help="Restore the source-branch version; leave this file out of the PR."):
                _decide(item, "deny")
            if retry_col.button("🔁 Retry", key=f"retry-{key}", width="stretch",
                                help="Have the agent fix the reviewer's issues and re-review this file."):
                _decide(item, "retry")
    if len(items) > 1:
        if st.button(f"✅ Approve all {len(items)} remaining", key="approve-all"):
            paths = [i["path"] for i in items]
            st.session_state.approved_files.update(paths)
            for p in paths:
                ui_events.approve_file(p)
            st.session_state.manual_reviews = []
            _queue_turn(
                "Approved as-is: " + ", ".join(f"`{p}`" for p in paths)
                + ". All flagged files are now decided; make no further changes to them."
            )


# --- chat ------------------------------------------------------------------------

def open_chat(agent, prompt, user_input):

    if "messages" not in st.session_state:
        st.session_state.messages = [SystemMessage(prompt)]
        # get bot summary
        message = "Summarize your instructions and get confirmation to proceed."
        # Persist the opening user turn too. Keeping only the reply left history
        # as [System, AI], and the next turn then sent an assistant-first
        # message list, which Bedrock rejects with a ValidationException.
        st.session_state.messages.append(HumanMessage(message))
        summary_convo = agent.invoke(st.session_state.messages)

        st.session_state.messages.append(summary_convo["messages"][-1])
        _collect_manual_reviews()
        _collect_secret_blocks()
        _collect_migration_plan()
        _collect_gate_refusals()
    for message in st.session_state.messages:
        if isinstance(message, SystemMessage): continue
        if isinstance(message, HumanMessage):
            with st.chat_message("user"):
                st.markdown(message.content)
        elif isinstance(message, AIMessage):
            with st.chat_message("assistant"):
                st.markdown(guardrails.redact(_as_text(message.content)))

    # Handle the message typed into the page-level chat input (or queued by a
    # review button), if any
    if user_input:

        st.session_state.offer_continue = False
        st.session_state.continue_reason = ""
        # 1. Append user message to history
        user_message = HumanMessage(user_input)
        st.session_state.messages.append(user_message)

        # Display user message in UI
        with st.chat_message("user"):
            st.markdown(user_input)

        # 2. Generate response from Bedrock, showing each step as it lands
        with st.chat_message("assistant"):
            status = st.status("Working…", expanded=True)
            response_placeholder = st.empty()

            def on_text(text):
                status.markdown(text)

            def on_tool_call(name, args):
                status.markdown(f"▶ **{name}** `{args}`")

            def on_tool_result(name, preview):
                status.markdown(f"◀ {name}: {preview}")

            def on_step():
                # Surface guardrail hits in the feed the moment the step lands.
                for event in _collect_secret_blocks():
                    status.error(_secret_block_markdown(event))
                _collect_migration_plan()
                _collect_gate_refusals()
            try:
                final = stream_turn(
                    agent, st.session_state.messages,
                    on_tool_call=on_tool_call, on_tool_result=on_tool_result,
                    on_text=on_text, on_step=on_step,
                )
                # Redact before it is shown or stored, so a secret the model
                # read from the repo never reaches the screen or the history.
                assistant_response = guardrails.redact(_as_text(final[-1].content))
                _collect_manual_reviews()
                _collect_secret_blocks()
                _collect_migration_plan()
                _collect_gate_refusals()
                status.update(label="Done", state="complete", expanded=False)
                response_placeholder.markdown(assistant_response)

                # Append assistant message to history
                st.session_state.messages.append(AIMessage(assistant_response))
                _offer_continue_if_unfinished()

            except StepLimitReached as e:
                # Not a failure: keep what the agent said so far and offer to resume.
                tool_calls = sum(len(m.tool_calls or []) for m in e.messages if isinstance(m, AIMessage))
                last_text = next((guardrails.redact(_as_text(m.content)).strip() for m in reversed(e.messages)
                                  if isinstance(m, AIMessage) and _as_text(m.content).strip()), "")
                note = (f"⏸ Paused: this turn reached the step limit ({settings.AGENT_RECURSION_LIMIT} steps, "
                        f"{tool_calls} tool calls). All work so far is kept in the working copy.")
                status.update(label="Paused at the step limit", state="complete", expanded=False)
                response_placeholder.warning(note + (f"\n\nLast update from the agent:\n\n{last_text}" if last_text else ""))
                _collect_manual_reviews(); _collect_secret_blocks(); _collect_migration_plan()
                _collect_gate_refusals()
                st.session_state.messages.append(AIMessage(
                    f"{note} Say 'continue' and I will call git_status on repo and resume from where the working copy stands."
                    + (f"\n\nLast progress note: {last_text}" if last_text else "")))
                st.session_state.offer_continue = True
            except ClientError as e:
                traceback.print_exc()
                status.update(label="Failed", state="error")
                error_message = e.response["Error"]["Message"]
                response_placeholder.error(f"AWS Bedrock Error: {error_message}")
            except Exception as e:
                traceback.print_exc()
                status.update(label="Failed", state="error")
                response_placeholder.error(f"An unexpected error occurred: {str(e)}")

    if st.session_state.get("offer_continue"):
        if st.session_state.get("continue_reason"):
            st.info(st.session_state.continue_reason)
        if st.button("▶ Continue where it left off", key="continue-turn", width="stretch"):
            st.session_state.offer_continue = False
            st.session_state.continue_reason = ""
            _queue_turn("Continue where you left off: call git_status and check_pack_acceptance on repo, "
                        "then finish the remaining MUST CHANGE files and the remaining steps of the plan.")

    # Rendered last so they always sit just above the input box.
    render_migration_plan()
    render_gate_refusal()
    render_secret_blocks()
    render_manual_reviews()


if __name__ == "__main__":
    logger.info("Starting upgrade...")
    agent, prompt = setup_upgrade_code()
    user_input = st.chat_input("Enter input here...")  # pinned to the page bottom
    # A review button sets pending_input and reruns; consume it as this turn's message.
    if not user_input:
        user_input = st.session_state.pop("pending_input", None)
    open_chat(agent, prompt, user_input)
