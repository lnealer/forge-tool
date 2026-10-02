from typing import Literal
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from pydantic import BaseModel, Field
from langgraph.types import interrupt, Command
from langgraph.checkpoint.memory import MemorySaver
from langchain.agents.structured_output import ToolStrategy
from langchain.agents import create_agent
from typing import Annotated, TypedDict
from langgraph.graph.message import add_messages

class AgentState(MessagesState):
    agent_messages: Annotated[list, add_messages]
    iterations: int
    auto_approved: bool
    human_approved: bool
    human_input_needed: bool
    review_status: Literal["pending", "max_iterations_reached"]
    max_iterations: int
    tmpdir: str

class WriterOutput(BaseModel):
    code_dir: str = Field(description="Path to directory where new code is written")
    message: str = Field(description="Notes on the current state of the review")

class ChatbotOutput(BaseModel):
    response: str = Field(description="Notes on the current state of the review")
    more_questions: bool = Field(description="True/false whether more human input is required")

class Graph:
    def __init__(self, reviewer, writer):
        self.reviewer=reviewer.llm
        self.writer=writer
    
    def conversation_node(self, state: AgentState):
        # Manages user dialogue and requirements gathering
        prompt = [HumanMessage(content="System: You are a requirements gathering chatbot for a code upgrade tool. " \
        "Write a message to the user based on the current state of the upgrade, including agent messages and review_status." \
        "If the status is auto_approved then it's time to get feedback from the user. If the status is max_iterations_reached tell the user you cannot continue.")]
        response = self.writer.with_structured_output(ChatbotOutput).invoke({"messages": prompt + state["messages"], "agentState": state})
        return {"messages": [AIMessage(response.message)], "human_input_needed": response.more_questions}

    def human_reply(self, state: AgentState):
        human_response = interrupt({
            "action": "give_input",
            "state": state,
        })

    def code_writer_node(self, state: AgentState):
        # Generates or refines code based on instructions
        messages = [HumanMessage(content="System: Write or update the code based on the conversation.")] + state["messages"] + state["agent_messages"]
        response = self.writer.unstructured_llm.with_structured_output(WriterOutput).invoke(messages)
        return {"agent_messages": ["Writer-Agent: "+ response], "iterations": state["iterations"]+1, "review_status": "pending"}

    def reviewer_node(self, state: AgentState):
        # Reviews code for issues and provides feedback
        iterations = state["iterations"]
        max_iterations = state["max_iterations"]

        prompt = [HumanMessage(content="System: Review the previous code for bugs, improvements, or correctness. Reply with feedback or approval.  If it is correct and meets all parameters, start your response with 'APPROVED'.")] 
       
        chain = prompt | self.reviewer
        response = chain.invoke(state["messages"])
        
        feedback = response.content
        auto_approved = feedback.strip().startswith("APPROVED") or iterations >= max_iterations 
        return {"agent_messages": ["Reviewer-Agent:" + response], "auto_approved": auto_approved}

    def route_after_auto_review(state: AgentState) -> Literal["human_conversation", "code_writer"]:
        if state["auto_approved"]:
            return "human_conversation"
        
        if state["iterations"] >= state["max_iterations"]:
            state["review_status"] = "max_iterations_reached"
            return "human_conversation"
            
        return "code_writer"

    def human_review_gate(state: AgentState):
        """Pauses graph execution to wait for a human developer's explicit approval or critique."""
        print(f"Automated Reviewer Notes:\n{state['review_feedback']}\n")
        
        # Interrupt triggers LangGraph persistence to save state and pause
        # The dictionary passed is the data payload sent to the UI/Frontend
        human_response = interrupt({
            "action": "review_code",
            "state": state,
        })
        
        # The return Command updates the state based on human UI input
        if human_response.get("approved", False):
            return {"human_approved": True, "review_feedback": ""}
        else:
            # Human rejected and provided correction details
            return {
                "human_approved": False, 
                "review_feedback": f"Human Reviewer rejected this with feedback: {human_response.get('feedback')}"
            }
    
    def human_input_needed(state: AgentState):
        if state["human_input_needed"]:
            return "human_conversation"
        return "code_writer"

    def route_after_human_gate(state: AgentState):
        if state["human_approved"]:
            return END
        return "code_writer"

    def initialize_graph(self, prompt):
        workflow = StateGraph(AgentState)

        workflow.add_node("human_conversation", self.conversation_node)
        workflow.add_node("code_writer", self.code_writer_node)
        workflow.add_node("reviewer", self.reviewer_node)
        workflow.add_node("human_review_gate", self.human_review_gate)

        # gather feedback loop
        workflow.add_edge(START, "human_conversation")
        workflow.add_conditional_edges("human_conversation", self.human_input_needed)
        workflow.add_edge("human_review_gate", "human_conversation")

        # code auto-review loop
        workflow.add_edge("code_writer", "reviewer")
        workflow.add_conditional_edges("reviewer", self.route_after_auto_review)

        # add human review gate
        workflow.add_conditional_edges("human_review_gate", self.route_after_human_gate)

        memory = MemorySaver()
        app = workflow.compile(checkpointer=memory)
        return app


# app.update_state(
#     config, 
#     {"review_status": "approved", "messages": [HumanMessage(content="Looks good, approve it!")]}, 
#     as_node="human_review_gate"
# )