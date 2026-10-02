import boto3
from botocore.client import Config
from langchain_aws import ChatBedrock
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from langchain.agents import create_agent
from git_utils import clone_repo, create_branch, create_pull_request, git_commit
from typing import List
from langchain_core.prompts import PromptTemplate
from langchain_community.agent_toolkits import FileManagementToolkit
import subprocess
from datetime import datetime
from colorama import Fore, Back, Style
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_aws.retrievers import AmazonKnowledgeBasesRetriever
from langchain_core.tools import create_retriever_tool
import streamlit as st
import asyncio

from utils import get_logger

config = Config(connect_timeout=240, read_timeout=240, retries={'total_max_attempts': 2,'max_attempts': 2})
# config = {"recursion_limit": 10}


logger = get_logger()

DEFAULT_MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_MODEL_REGION = "us-east-1";
REVIEWER_MODEL ="global.amazon.nova-pro-v1:0"
PROMPT_TEMPLATE = """
You are a code upgrading assistant tool for Spring, Java, Spring Boot, and Struts.
Clone the repo inside the tmpdir using repo_url. Upgrade the code and push to a branch. 
Format the branch name like 'forge-upgrade-[current timestamp]'.
Then create a PR using repo_api_url and report any remaining issues in the PR description.
Modify only the code relevant to the upgrade.
Censor API keys in messages.
Before making code changes, check the knowledge base for relevant guidelines. Only use guidelines relevant to your change.
After writing the upgrades, run unit tests for all modules. Check the output and fix any issues. Report issues you can't fix. Include the test results in the PR description.
When the user asks for changes, make the relevant changes to the code and then push to the remote for review.
Push additional changes to your original branch.

<verion>
{version}
</version>
<repo_url>
{repo_url}
</repo_url>
<api_key>
{api_key}
</api_key>
<ssh_private_key_path>
{ssh_private_key_path}
</ssh_private_key_path>
<tmpdir>
{tmpdir}
</tmpdir>
"""


class Model:
    """Model class for GenAI."""

    def invoke(self, message, conversation):
        conversation.append(HumanMessage(message))
        conversation = self.llm.invoke({"messages": conversation})
        return conversation
    
    def invoke(self, conversation):
        return self.llm.invoke({"messages": conversation})

class NovaPro(Model):
    def __init__(self, model_id=REVIEWER_MODEL, model_aws_region=DEFAULT_MODEL_REGION, working_dir=""):
        logger.info(f"Initializing NovePro with model_id: {model_id} and region: {model_aws_region}")
        bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=model_aws_region,
            config=config
        )

        # Create agent with tools
        self.unstructured_llm = ChatBedrock(
            client=bedrock_client,
            region_name = model_aws_region,
            model_id=model_id,
            max_tokens=10000,
            model_kwargs={
                "temperature": 0.0,
                "max_tokens": 10000,
            },
        )

        toolkit = FileManagementToolkit(root_dir=working_dir, selected_tools=["read_file", "list_directory"])
        tools = toolkit.get_tools()
        kb_tool = load_kb_tool()
        llm =  self.unstructured_llm.bind_tools([clone_repo, create_branch, get_current_timestamp, git_commit, kb_tool] + tools)
        self.llm = create_agent(model=llm, tools=[clone_repo, create_branch, get_current_timestamp, git_commit, kb_tool] + tools)
        logger.info("Initialized Nova Pro")


class Claude(Model):
    """Claude model class."""

    def __init__(self, model_id=DEFAULT_MODEL, model_aws_region=DEFAULT_MODEL_REGION, working_dir=""):
        logger.info(f"Initializing Claude with model_id: {model_id} and region: {model_aws_region}")
        bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=model_aws_region,
            config=config
        )

        # Create agent with tools
        unstructured_llm = ChatBedrock(
            client=bedrock_client,
            region_name = model_aws_region,
            model_id=model_id,
            max_tokens=10000,
            model_kwargs={
                "temperature": 0.0,
                "max_tokens": 10000
            },
        )

        toolkit = FileManagementToolkit(root_dir=working_dir, selected_tools=["read_file", "write_file", "list_directory"])
        tools = toolkit.get_tools()
        kb_tool = load_kb_tool()
        llm =  unstructured_llm.bind_tools([run_maven_test, create_pull_request, clone_repo, create_branch, get_current_timestamp, git_commit, kb_tool] + tools)
        self.llm = create_agent(model=llm, tools=[run_maven_test, create_pull_request, clone_repo, create_branch, get_current_timestamp, git_commit, kb_tool] + tools)
        logger.info("Initialized Claude")

    def create_prompt(self, version, repo_api_url, repo_url, api_key, ssh_private_key_path, tmpdir):
        """Create a prompt for the model to generate a code upgrade."""
        logger.info("Creating prompt for model")
        prompt = PROMPT_TEMPLATE.format(version=version, repo_api_url=repo_api_url, repo_url=repo_url, api_key=api_key, ssh_private_key_path=ssh_private_key_path, tmpdir=tmpdir)
        return prompt


    def _invoke(self, prompt):
        """Invoke the model with the prompt."""
        response = self.llm.invoke(prompt)
        # Append opening curly braces which might be missing, depending on the prompt.
        logger.info(f"Raw response from GenAI: {response}")
        # if not response.startswith("{"):
        #     response = "{" + response
        return response
    
    def _invoke_unstructured(self, prompt):
        """Invoke the model with the prompt."""
        response = self.unstructured_llm.invoke(prompt)
        logger.info(f"Raw response from GenAI: {response}")
        return response

# TODO:
# Add chat tool for getting user input
# Add second reviewer agent
# Add test harness tools   
# 
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
    command = f'mvn -U clean test -f {code_dir}'
    try:
        # Capture the output and check the return code
        result = subprocess.run(command, check=True, shell=True, capture_output=True, text=True)
        logger.info(f"Mvn output: {result.stdout}")
        return result.stdout if result.returncode == 0 else result.stderr
    except subprocess.CalledProcessError as e:
        logger.info(f"Command '{command}' failed with return code {e.returncode}")
        logger.info(f"Mvn output: {e.stdout}")
        return e.stdout

@tool
def run_maven_compile(code_dir: str) -> str:
    """Runs a shell command using subprocess to compile application and returns output.
    Args:
        code_dir: The code directory to execute from.
    """
    logger.info(f"Running maven compile {code_dir}")
    command = f'mvn -U clean package -f {code_dir}'
    try:
        # Capture the output and check the return code
        result = subprocess.run(command, check=True, shell=True, capture_output=True, text=True)
        logger.info(f"Mvn output: {result.stdout}")
        return result.stdout if result.returncode == 0 else result.stderr
    except subprocess.CalledProcessError as e:
        logger.info(f"Command '{command}' failed with return code {e.returncode}")
        logger.info(f"Mvn output: {e.stdout}")
        return e.stdout

@tool
def get_current_timestamp():
    """ Get the current date and time """
    return datetime.now()    
