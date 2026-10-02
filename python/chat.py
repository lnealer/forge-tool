
from utils import get_logger
from config_upgrade_code import setup_upgrade_code
from agent_graph import INITIAL_STATE
import streamlit as st
from botocore.exceptions import ClientError
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
import traceback
import asyncio
from pydantic import BaseModel, Field
from langgraph.types import Command

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
        
    if st.session_state.interrupted:
        logger.info("Getting human review")
        human_review(graph)
        return

    if st.session_state.coding:
        logger.info("Waiting for code...")
        return


    # chatting with user
    logger.info("Getting user input...")
    summarize_upgrade(graph)

def render_messages():
    for message in st.session_state.messages:
        if isinstance(message, SystemMessage): continue
        if isinstance(message, HumanMessage):
            with st.chat_message("user"):
                st.markdown(message.content)
        elif isinstance(message, AIMessage):
            with st.chat_message("assistant"):
                st.markdown(message.content)

def render_last_message():
    message = st.session_state.messages[-1]
    if isinstance(message, SystemMessage): return
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
    if "upgrade_done" not in st.session_state:
        st.session_state.upgrade_done = False
    if "coding" not in st.session_state:
        st.session_state.coding = False
    if "messages" not in st.session_state:
        st.session_state.messages = []
        # get bot summary
        message = "Briefly summarize the upgrade task (versions, repo, etc.) and get confirmation to proceed with coding. The user can enter 'proceed' to begin the coding process, or enter their feedback to edit the plan."
        summary_convo = st.session_state.messages + [HumanMessage(message)]
        summary_convo = chatbot.invoke({"human_conversation": summary_convo})

        st.session_state.messages.append(summary_convo["messages"][-1])

    render_messages()

    if "agent_state" not in st.session_state:
        st.session_state.agent_state = INITIAL_STATE.copy()
        st.session_state.agent_state["repo_path"]=repo_path
        st.session_state.agent_state["branch_name"]=branch_name
        logger.info(st.session_state.agent_state)


def summarize_upgrade(graph):
    # gets user input and invokes the graph when ready to execute
    if user_input := st.chat_input("Enter message here...", disabled=st.session_state.upgrade_done or st.session_state.coding):
    
        user_message = HumanMessage(user_input)
        st.session_state.messages.append(user_message)
        
        # Display user message in UI
        with st.chat_message("user"):
            st.markdown(user_input)

        # 2. Generate response from Bedrock
        with st.chat_message("assistant"):
            try:           
                if (user_input.lower() == "proceed"): # todo - could be a button instead
                    asyncio.run(invoke_graph(graph))
                    st.rerun()               
                else:
                    response_placeholder = st.empty()
                    response_placeholder.markdown("*Thinking...*")
                    response = chatbot.invoke({"human_conversation": st.session_state.messages})
                    # Update UI with the final answer
                    assistant_response = response.message
                    response_placeholder.markdown(assistant_response)
                    
                    # Append assistant message to historyzza
                    st.session_state.messages.append(AIMessage(assistant_response))
                
            except ClientError as e:
                traceback.print_exc()
                error_message = e.response["Error"]["Message"]
                response_placeholder.error(f"AWS Bedrock Error: {error_message}")
            except Exception as e:
                traceback.print_exc()
                response_placeholder = st.empty()
                response_placeholder.error(f"An unexpected error occurred: {str(e)}")

# invoke graph execution of the current task
async def invoke_graph(graph):
    response_placeholder = st.empty()
    response_placeholder.markdown("*Coding...*")
    st.session_state.coding = True
    st.session_state.agent_state["messages"] = st.session_state.messages
    logger.info(st.session_state.agent_state)
    # wait for the final output of the stream
    result = await graph.ainvoke(st.session_state.agent_state, config=st.session_state.config, version="v3")        
    snapshot= await graph.aget_state(st.session_state.config)
    st.session_state.coding = False
    handle_graph_result(snapshot)
    st.rerun()
    
async def resume_graph(resume_command, graph):
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
        st.session_state.messages.append(AIMessage("Code is ready for review!"))

def get_coding_summary():
        message = "Briefly summarize your instructions and get confirmation to proceed with coding."
        summary_convo = st.session_state.messages + [SystemMessage(message)]
        summary_convo = chatbot.invoke(summary_convo)

def human_review(graph):
    logger.info("Human review phase...")

    if user_input := st.chat_input("Enter feedback here or APPROVED to approve code...", disabled=st.session_state.upgrade_done or st.session_state.coding):
            user_message = HumanMessage(user_input)
            st.session_state.messages.append(user_message)
            
            # Display user message in UI
            with st.chat_message("user"):
                st.markdown(user_input)

            approved = "APPROVED" in user_input.upper()
            with st.chat_message("assistant"):
                try:
                    # todo: should this be convo history?
                    if (approved): # todo - could be a button instead
                        asyncio.run(resume_graph(Command(resume={"approved": approved}), graph))
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

if __name__ == "__main__":
    # logger.info("Starting upgrade...")
    writer, chatbot, reviewer, graph,branch_name,path= setup_upgrade_code()
    loop(chatbot, graph,branch_name,path)
