
from utils import get_logger
from config_upgrade_code import setup_upgrade_code
from agent_graph import INITIAL_STATE
import streamlit as st
from botocore.exceptions import ClientError
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage,ToolMessage
import traceback
import asyncio
from pydantic import BaseModel, Field
from langgraph.types import Command
from langgraph.errors import GraphRecursionError
import ui_events
import json

st.set_page_config(page_title="Forge Chatbot", page_icon="🤖", layout="centered")
st.title("🤖 Forge Chatbot")
st.caption("Powered by AWS Bedrock & Streamlit")

logger = get_logger()

# Chat with user about code upgrade reqs ->  write code -> get human review -> repeat as needed
class ChatBotOutput(BaseModel):
    message: str = Field(description="Chat response to the user")
    ready_for_code: bool = Field(description="True/false whether to start graph or get more feedback from the user")

# main application loop- handle chatting with user and waiting for graph to execute
def loop(chatbot, graph, branch_name, repo_path):
    initialize_session_state(chatbot, repo_path,branch_name)
    render_messages()
    render_migration_plan()
    
    user_input = st.chat_input("Enter input here...", disabled=st.session_state.upgrade_done or st.session_state.coding)

    if st.session_state.messages==[]:
        logger.info("initial planning phase")
        get_coding_plan(chatbot)
        st.rerun()


    if st.session_state.upgrade_done:
        st.stop()
        return
        
    if st.session_state.interrupted:
        logger.info("Getting human review")
        human_review(user_input,graph)
        return
    
    if st.session_state.coding:
        logger.info("Waiting for code...")
        return
    if st.session_state.plan_complete:
        # begin coding
        logger.info("Starting to code...")
        asyncio.run(invoke_graph(graph))
        return

    # chatting with user
    logger.info("Planning upgrade...")
    plan_upgrade(user_input,graph)

def render_messages():
    for message in st.session_state.messages:
        if isinstance(message, SystemMessage): continue
        if isinstance(message, HumanMessage):
            with st.chat_message("user"):
                st.markdown(message.content)
        elif isinstance(message, AIMessage):
            with st.chat_message("assistant"):
                st.markdown(message.content)

def initialize_session_state(chatbot,repo_path,branch_name): 
    if "interrupted" not in st.session_state:
        st.session_state.interrupted=False
    if "config" not in st.session_state:
        st.session_state.config = {"configurable": {"thread_id": "1"}, "recursion_limit": 200}
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "upgrade_done" not in st.session_state:
        st.session_state.upgrade_done = False
    if "coding" not in st.session_state:
        st.session_state.coding = False
    if "plan_complete" not in st.session_state:
        st.session_state.plan_complete=False
    if "agent_state" not in st.session_state:
        st.session_state.agent_state = INITIAL_STATE.copy()
        st.session_state.agent_state["repo_path"]=repo_path
        st.session_state.agent_state["branch_name"]=branch_name
        logger.info(st.session_state.agent_state)
    if "migration_plan" not in st.session_state:
        st.session_state.migration_plan=None

def plan_upgrade(user_input,graph):
    # gets user input and invokes the graph when ready to execute
    if user_input:
    
        user_message = HumanMessage(user_input)
        st.session_state.messages.append(user_message)
        
        # Display user message in UI
        with st.chat_message("user"):
            st.markdown(user_input)

        if user_input.lower() != "proceed" or not st.session_state.plan_complete:
            # keep planning
            invoke_chat(chatbot)
                
# invoke graph execution of the current task
async def invoke_graph(graph):
    with st.chat_message("assistant"):
        response_placeholder = st.empty()
        if not st.session_state.upgrade_done:
            response_placeholder.markdown("*Coding...*")
        st.session_state.coding = True
        st.session_state.agent_state["messages"] = st.session_state.messages
        st.session_state.agent_state["migration_plan"] = st.session_state.migration_plan
        logger.info(st.session_state.agent_state)
        
        # wait for the final output of the stream
        try:
            result = await graph.ainvoke(st.session_state.agent_state, config=st.session_state.config, version="v3")        
            snapshot= await graph.aget_state(st.session_state.config)
        except GraphRecursionError as e:
            st.session_state.graph_maxed=True
        
        st.session_state.coding = False
        handle_graph_result(snapshot)
        st.rerun()
    
async def resume_graph(resume_command, graph, coding=True):
    with st.chat_message("assistant"):
        response_placeholder = st.empty()
        response_placeholder.markdown("*Coding...*")
    
        st.session_state.coding = True
        st.session_state.agent_state["human_messages"] = st.session_state.messages
        
        result=await graph.ainvoke(resume_command, st.session_state.config, stream_mode="updates")
        snapshot = await graph.aget_state(st.session_state.config)
        st.session_state.coding = False
        
        handle_graph_result(snapshot)
        st.rerun()

def handle_graph_result(state_snapshot):
    st.session_state.interrupted=False # check for interrupts
    if state_snapshot.tasks:
        current_task = state_snapshot.tasks[0]
        if current_task.interrupts:
            st.session_state.interrupted=True
            logger.info("Graph interrupted...")
    if not st.session_state.interrupted:
        # otherwise - upgrade is done
        logger.info("Upgrade done!")
        st.session_state.upgrade_done = True
        st.session_state.messages.append(AIMessage("Upgrade is complete."))
    else:
        st.session_state.messages.append(AIMessage("Code is ready for review! Enter feedback or APPROVED to finish upgrade."))

def get_coding_plan(chatbot):
        message = "Create the detailed migration plan using propose_migration_plan."
        summary_convo = st.session_state.messages + [HumanMessage(message)]
        summary_convo = invoke_chat(chatbot, summary_convo)
        # st.session_state.messages.append(summary_convo["messages"][-1])
        _collect_migration_plan()

def human_review(user_input,graph):
    logger.info("Human review phase...")

    if not user_input:
        return

    user_message = HumanMessage(user_input)
    st.session_state.messages.append(user_message)
    
    # Display user message in UI
    with st.chat_message("user"):
        st.markdown(user_input)

    approved = "APPROVED" in user_input.upper()
    try:
        if approved: # todo - could be a button instead
            logger.info("Upgrade is approved - done")
            asyncio.run(resume_graph(Command(resume={"approved": approved}), graph, coding=False))
            st.rerun()               
        else:
            asyncio.run(resume_graph(Command(resume={"approved": approved, "feedback": user_message}), graph))
            st.rerun()
        
    except ClientError as e:
        traceback.print_exc()
        error_message = e.response["Error"]["Message"]
        response_placeholder.error(f"AWS Bedrock Error: {error_message}")
    except Exception as e:
        traceback.print_exc()
        response_placeholder = st.empty()
        response_placeholder.error(f"An unexpected error occurred: {str(e)}")

# --- migration plan ------------------------------------------------------------

def _collect_migration_plan():
    """Take a plan the agent proposed via propose_migration_plan into the session."""
    plan = ui_events.take_migration_plan()
    if not plan:
        logger.info("Collect found no plan")
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
    logger.info("rendering migration plan")
    plan = st.session_state.get("migration_plan")
    if not plan:
        return
    items = plan.get("items", [])
    goal = plan.get("goal")
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
        approve_col, = st.columns(1)
        
        # approve as-is
        if approve_col.button(f"Approve plan", key="approve-plan", width="stretch",
                              help="Migrate the in-scope items exactly as proposed."):
            plan["status"] = "approved"
            ui_events.approve_packs([i.get("pack", "") for i in items if i.get("in_scope", True)])
            st.session_state.messages.append(SystemMessage(
                "Plan approved. Migrate the in-scope items exactly as proposed (optional items stay out), "
                "then run the tests and the review.")
            )
            st.session_state.plan_complete=True

        # # approve optional
        # if n_opt and all_col.button(f"➕ Approve incl. {n_opt} optional item(s)", key="approve-plan-all",
        #                             width="stretch", help="Also migrate the optional candidates."):
        #     for i in items:
        #         i["in_scope"] = True
        #     plan["status"] = "approved"
        #     ui_events.approve_packs([i.get("pack", "") for i in items])
        #     st.session_state.plan_complete=True
        #     st.session_state.messages.append(SystemMessage(
        #         "Plan approved including the optional items: migrate every item in the plan, "
        #         "then run the tests and the review.")
        #     )
        st.caption("To change the plan, describe the change in the chat and the agent will re-propose it.")

def invoke_chat(chatbot,messages=None):
    messages = messages or st.session_state.messages
    with st.chat_message("assistant"):
            response_placeholder = st.empty()
            response_placeholder.markdown("*Planning...*")
            try:
                new_msgs = chatbot.invoke({"human_conversation": messages})
                # Redact before it is shown or stored, so a secret the model
                # read from the repo never reaches the screen or the history.
                assistant_response = new_msgs["messages"][-1]

                # Append assistant message to history
                # todo- do I want to do this?
                st.session_state.messages.append(assistant_response)
                _collect_migration_plan()
            except ClientError as e:
                traceback.print_exc()
                error_message = e.response["Error"]["Message"]
                response_placeholder.error(f"AWS Bedrock Error: {error_message}")
            except Exception as e:
                traceback.print_exc()
                response_placeholder.error(f"An unexpected error occurred: {str(e)}")

if __name__ == "__main__":
    writer, chatbot, reviewer, graph,branch_name,path= setup_upgrade_code()
    loop(chatbot, graph,branch_name,path)
