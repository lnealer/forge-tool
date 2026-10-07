from typing import Literal
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from utils import get_logger
from langgraph.types import interrupt
from langgraph.checkpoint.memory import MemorySaver
from typing import Annotated, TypedDict, Any
from langgraph.graph.message import add_messages
import streamlit as st
from langchain_core.messages import AnyMessage

logger = get_logger()


class AgentState(TypedDict):
    iterations: int
    max_iterations: int
    messages: Annotated[list[AnyMessage], add_messages] 
    writer_notes: str
    reviewer_notes: str
    repo_path: str
    branch_name: str

    human_approved: bool
    auto_approved: bool


    #################################
    # agent_messages: Annotated[list, add_messages]
    # human_approved: bool
    # human_input_needed: bool
    # review_status: Literal["pending", "max_iterations_reached"]
    # max_iterations: int
INITIAL_STATE: AgentState = {
    "iterations": 0,
    "max_iterations": 10,
    "writer_notes": "",
    "reviewer_notes": "",
    "messages": [],
    "human_approved": False,
    "auto_approved": False,
}
# class WriterOutput(BaseModel):
#     code_dir: str = Field(description="Path to directory where new code is written")
#     message: str = Field(description="Notes on the current state of the review")

class ChatbotOutput(BaseModel):
    response: str = Field(description="Notes on the current state of the review")
    more_questions: bool = Field(description="True/false whether more human input is required")

class Graph:
    def __init__(self, reviewer, writer):
        self.reviewer=reviewer
        self.writer=writer

    def code_writer_node(self, state: AgentState):
        # Generates or refines code based on instructions

        reviewer_notes = state["reviewer_notes"]
        writer_notes=""
        messages = state["messages"] 
        repo_path = state["repo_path"]
        branch_name=state["branch_name"]
        logger.info(messages[-1])

        count = 0
        writer_note=""
        try:
            response = self.writer.invoke({"human_conversation": messages, 
                                        "reviewer_notes": reviewer_notes, 
                                        "writer_notes": writer_notes,
                                        "repo_path": repo_path,
                                        "branch_name": branch_name,
                                        })
            writer_note=response["messages"][-1]
        except Exception as e:
            count+=1
            if (count>3):
                raise e
        return {"writer_notes": writer_note, 
                "iterations": state["iterations"] + 1,
                }

    def reviewer_node(self, state: AgentState):
        # Reviews code for issues and provides feedback
        logger.info("Reviewing...")
        iterations = state["iterations"]
        max_iterations = state["max_iterations"]
        messages = state["messages"] 
        repo_path = state["repo_path"]
        writer_notes = state["writer_notes"]

        count = 0
        feedback = ""
        feedback_content=""
        try:
            response = self.reviewer.invoke({"human_conversation": messages, 
                                            "repo_path": repo_path,
                                            "writer_notes": writer_notes,
                                            })
            feedback = response["messages"][-1]
            feedback_content = feedback.content
        except Exception as e:
            count+=1
            if (count>3):
                raise e
        
        auto_approved = "APPROVED" in feedback_content or iterations >= max_iterations 
        return {"reviewer_notes": feedback, "auto_approved": auto_approved}

    def route_after_auto_review(self,state: AgentState) -> Literal["human_review_gate", "code_writer"]:
        if state["auto_approved"]:
            return "human_review_gate"
        
        if state["iterations"] >= state["max_iterations"]:
            state["review_status"] = "max_iterations_reached"
            return "human_review_gate"
            
        return "code_writer"

    def human_review_gate(self,state: AgentState):
        """Pauses graph execution to wait for a human developer's explicit approval or critique."""
        print(f"Automated Reviewer Notes:\n{state['reviewer_notes']}\n")
        
        # Interrupt triggers LangGraph persistence to save state and pause
        # The dictionary passed is the data payload sent to the UI/Frontend
        human_response = interrupt({
            "action": "review_code",
        })
        
        logger.info("Resuming...")
        if human_response.get("approved", False):
            logger.info("Human approved!")
            return {"human_approved": True}
        else:
            # Human rejected and provided correction details
            logger.info(f"Human provided this feedback:  {human_response.get('feedback')}")
            return {
                "human_approved": False, 
                "messages": [human_response.get('feedback')]
            }

    def route_after_human_gate(self,state: AgentState) -> Literal["code_writer", END]: 
        if state["human_approved"]:
            logger.info("exiting graph")
            return END
        return "code_writer"

    def initialize_graph(self):
        workflow = StateGraph(AgentState)

        workflow.add_node("code_writer", self.code_writer_node)
        workflow.add_node("reviewer", self.reviewer_node)
        workflow.add_node("human_review_gate", self.human_review_gate)

        # code writer and auto-review loop
        workflow.add_edge(START, "code_writer")
        workflow.add_edge("code_writer", "reviewer")
        workflow.add_conditional_edges("reviewer", self.route_after_auto_review)

        # human review gate
        workflow.add_conditional_edges("human_review_gate", self.route_after_human_gate)

        memory = MemorySaver()
        app = workflow.compile(checkpointer=memory)
        return app