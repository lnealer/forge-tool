import boto3
from botocore.client import Config
from langchain_aws import ChatBedrock
from langchain.agents import create_agent
from git_utils import clone_repo, create_branch, create_pull_request, git_commit,update_pull_request,get_active_branch_name
from langchain_community.agent_toolkits import FileManagementToolkit
from langchain_core.prompts import ChatPromptTemplate
from tools import load_kb_tool, run_maven_test, run_maven_compile, get_current_timestamp

from utils import get_logger

config = Config(connect_timeout=240, read_timeout=240, retries={'total_max_attempts': 5,'max_attempts': 5})
# config = {"recursion_limit": 10}

logger = get_logger()

DEFAULT_WRITER_MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_MODEL_REGION = "us-east-1";
DEFAULT_REVIEWER_MODEL ="amazon.nova-pro-v1:0"

WRITER_PROMPT_TEMPLATE = """
SYSTEM: You are a code upgrading assistant tool for Spring, Java, Spring Boot, and Struts.
The repo is cloned inside the tmpdir. Upgrade the code and push. 
Modify only the code relevant to the upgrade. Also upgrade test code if required for upgrade.
Before making code changes, check the knowledge base for relevant guidelines. Only use guidelines relevant to your change.
To verify, compile then run unit tests for all modules. Check the output and fix any issues. Report issues you can't fix. Include the test results in the PR description.
Commit changes then create a PR using repo_api_url and report any remaining issues in the PR description.
When the user or reviewer agent asks for changes, make the relevant changes to the code and then push to the remote for review.

<verion>
{version}
</version>
<repo_url>
{repo_url}
</repo_url>
<ssh_private_key_path>
{ssh_private_key_path}
</ssh_private_key_path>
<tmpdir>
{tmpdir}
</tmpdir>
"""

CHATBOT_PROMPT_TEMPLATE = """
You are a code upgrade summary chat bot for Spring, Java, Spring Boot, and Struts.
You communicate very clearly and in brief, concise statements.

Upgrade info:
<verion>
{version}
</version>
<repo_url>
{repo_url}
</repo_url>
<ssh_private_key_path>
{ssh_private_key_path}
</ssh_private_key_path>
<tmpdir>
{tmpdir}
</tmpdir>
"""

REVIEWER_PROMPT_TEMPLATE = """
System: You are a reviewing agent for Spring, Java, and Struts upgrades. Review the previously generated code for bugs, improvements, or correctness. Reply with feedback or approval.  If it is correct and meets all parameters, start your response with 'APPROVED'.
The generated code can be found in the tmpdir.

<verion>
{version}
</version>
<verion>
{version}
</version>
<repo_url>
{repo_url}
</repo_url>
<ssh_private_key_path>
{ssh_private_key_path}
</ssh_private_key_path>
<tmpdir>
{tmpdir}
</tmpdir>
"""


class Model:
    """Model class for GenAI."""

    def invoke(self, input):
        response = self.llm_chain.invoke(input) 
        logger.info(response["messages"][-1])  
        return response

class NovaPro(Model):
    def __init__(self, model_id=DEFAULT_REVIEWER_MODEL, model_aws_region=DEFAULT_MODEL_REGION, working_dir="", tools=[]):
        nova_config = Config(connect_timeout=240, read_timeout=240, retries={'total_max_attempts': 1,'max_attempts': 1})

        logger.info(f"Initializing NovePro with model_id: {model_id} and region: {model_aws_region}")
        bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=model_aws_region,
            config=nova_config
        )

        # Create agent with tools
        self.unstructured_llm = ChatBedrock(
            client=bedrock_client,
            region_name = model_aws_region,
            model_id=model_id,
            max_tokens=7500,
            model_kwargs={
                "temperature": 0.0,
                "max_tokens": 7500,
            },
        )


        llm =  self.unstructured_llm.bind_tools(tools)
        self.llm = create_agent(model=llm, tools=tools)
        logger.info("Initialized Nova Pro")


class Claude(Model):
    """Claude model class."""

    def __init__(self, model_id=DEFAULT_WRITER_MODEL, model_aws_region=DEFAULT_MODEL_REGION, working_dir="",tools=[]):
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
                "temperature": 0.2,
                "max_tokens": 10000
            },
        )

        llm =  unstructured_llm.bind_tools(tools)
        self.llm = create_agent(model=llm, tools=tools)
        logger.info("Initialized Claude")


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

class Reviewer(NovaPro):
        def __init__(self, model_id=DEFAULT_REVIEWER_MODEL, model_aws_region=DEFAULT_MODEL_REGION, working_dir="", tools=None,prompt=""):
            self.prompt=prompt
            # create tools
            if tools == None:
                file_toolkit = FileManagementToolkit(root_dir=working_dir, selected_tools=["read_file", "list_directory"])
                file_tools = file_toolkit.get_tools()
                kb_tool = load_kb_tool()
                tools =[kb_tool] + file_tools

            super().__init__(model_id=model_id,model_aws_region=model_aws_region,working_dir=working_dir,tools=tools)
            self.template = ChatPromptTemplate.from_messages([
                ("system", prompt),
                ("human", "Repo path: {repo_path}"),
                ("human", "{human_conversation}"),
                ("system", "Writer notes from past round: {writer_notes}"),
            ])
            self.llm_chain = self.template | self.llm

class Writer(Claude):
        def __init__(self, model_id=DEFAULT_WRITER_MODEL, model_aws_region=DEFAULT_MODEL_REGION, working_dir="", tools=None,prompt=""):
            self.prompt = ChatPromptTemplate.from_messages([
                ("system", prompt + "\n" + "Reviewer Feedback from previous turn: {reviewer_notes}"),
                ("system", "Writer notes from past round: {writer_notes}"),
                ("human", "Repo path: {repo_path}, branch name: {branch_name}"),
                ("human", "Human conversation: {human_conversation}"),
            ])
            # create tools
            if tools == None:
                file_toolkit = FileManagementToolkit(root_dir=working_dir, selected_tools=["read_file", "write_file", "list_directory"])
                file_tools = file_toolkit.get_tools()
                kb_tool = load_kb_tool()
                tools = [kb_tool, get_active_branch_name,run_maven_test, run_maven_compile, create_pull_request, update_pull_request, get_current_timestamp, git_commit] + file_tools
            super().__init__(model_id=model_id,model_aws_region=model_aws_region,working_dir=working_dir,tools=tools)
            self.llm_chain = self.prompt | self.llm

class ChatBot(Claude):
        def __init__(self, model_id=DEFAULT_WRITER_MODEL, model_aws_region=DEFAULT_MODEL_REGION, working_dir="", tools=None,prompt=""):
            # create tools
            if tools == None:
                file_toolkit = FileManagementToolkit(root_dir=working_dir, selected_tools=["read_file", "list_directory"])
                file_tools = file_toolkit.get_tools()
                kb_tool = load_kb_tool()
                tools = [kb_tool] + file_tools
            super().__init__(model_id=model_id,model_aws_region=model_aws_region,working_dir=working_dir,tools=tools)
            self.prompt = ChatPromptTemplate.from_messages([
                ("system", prompt),
                ("human", "{human_conversation}")
            ])
            self.llm_chain = self.prompt | self.llm


def create_prompt(prompt_template, version, repo_api_url, repo_url, ssh_private_key_path, tmpdir):
    """Create a prompt for the model to generate a code upgrade."""
    logger.info("Creating prompt for model")
    prompt = prompt_template.format(version=version, repo_api_url=repo_api_url, repo_url=repo_url, ssh_private_key_path=ssh_private_key_path, tmpdir=tmpdir)
    return prompt