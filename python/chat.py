
from utils import get_logger, get_config
from config_upgrade_code import setup_upgrade_code
import streamlit as st
from botocore.exceptions import ClientError
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
import traceback

st.set_page_config(page_title="Forge Chatbot", page_icon="🤖", layout="centered")
st.title("🤖 Forge Chatbot")
st.caption("Powered by AWS Bedrock & Streamlit")

logger = get_logger()


def open_chat(agent, prompt):

    if "messages" not in st.session_state:
        st.session_state.messages = [SystemMessage(prompt)]
        # get bot summary
        message = "Summarize your instructions and get confirmation to proceed."
        summary_convo = st.session_state.messages + [HumanMessage(message)]
        summary_convo = agent.invoke(summary_convo)
        st.session_state.messages.append(summary_convo["messages"][-1])

    for message in st.session_state.messages:
        if isinstance(message, SystemMessage): continue
        if isinstance(message, HumanMessage):
            with st.chat_message("user"):
                st.markdown(message.content)
        elif isinstance(message, AIMessage):
            with st.chat_message("assistant"):
                st.markdown(message.content)

    # Accept User Input
    if user_input := st.chat_input("Enter input here..."):
        
        # 1. Append user message to history
        user_message = HumanMessage(user_input)
        st.session_state.messages.append(user_message)
        
        # Display user message in UI
        with st.chat_message("user"):
            st.markdown(user_input)

        # 2. Generate response from Bedrock
        with st.chat_message("assistant"):
            response_placeholder = st.empty()
            response_placeholder.markdown("*Thinking...*")
            
            try:
                response = agent.invoke(st.session_state.messages)
                
                # Extract output text
                assistant_response = response["messages"][-1].content
                
                # Update UI with the final answer
                response_placeholder.markdown(assistant_response)
                
                # Append assistant message to history
                st.session_state.messages.append(AIMessage(assistant_response))
                
            except ClientError as e:
                traceback.print_exc()
                error_message = e.response["Error"]["Message"]
                response_placeholder.error(f"AWS Bedrock Error: {error_message}")
            except Exception as e:
                traceback.print_exc()
                response_placeholder.error(f"An unexpected error occurred: {str(e)}")


if __name__ == "__main__":
    logger.info("Starting upgrade...")
    agent, prompt = setup_upgrade_code()
    open_chat(agent, prompt)
