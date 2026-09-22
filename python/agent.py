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


from utils import get_logger

config = Config(connect_timeout=240, read_timeout=240)


logger = get_logger()

DEFAULT_MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_MODEL_REGION = "us-east-1";
PROMPT_TEMPLATE = """
You are a code upgrading assistant chat bot for Spring, Java, Spring Boot, and Struts.
Clone the repo inside the tmpdir using repo_url. Upgrade the code and push to a branch. 
Format the branch name like 'spring-upgrade-[current timestamp]'.
After writing the upgrades run unit tests using maven and address any issues.
Then create a PR using repo_api_url and report any remaining issues in the PR description.
Modify only the code relevant to the upgrade.
Respond with a message when you're ready for a review. 
When the user asks for changes, mke the relevant changes to the code and push to your pull request.
Censor API keys in messages.
When you get feedback, push the changes to the remote for review.

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
        conversation = self.llm.invoke({"messages": conversation})
        return conversation

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
            model_kwargs={
                "temperature": 0.0,
                "max_tokens": 10000,
                # "top_p": 0.999,
                # "top_k": 250,
                # "stop_sequences": [
                #     "\\n\\nHuman::",
                # ],
            },
        )

        toolkit = FileManagementToolkit(root_dir=working_dir, selected_tools=["read_file", "write_file", "list_directory"])
        tools = toolkit.get_tools()
        llm =  unstructured_llm.bind_tools([run_maven_test, update_source_code, create_pull_request, clone_repo, create_branch, get_current_timestamp, git_commit] + tools)
        self.llm = create_agent(model=llm, tools=[run_maven_test, update_source_code, create_pull_request, clone_repo, create_branch, get_current_timestamp, git_commit] + tools)
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
@tool
def run_maven_test(code_dir: str) -> str:
    """Runs a shell command using subprocess and handles potential errors.

    Args:
        code_dir: The code directory to execute unit tests from.
    """
    logger.info(f"Running: mvn clean test -f {code_dir}")
    command = f'mvn clean test -f {code_dir}'
    try:
        # Capture the output and check the return code
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        return result.stdout if result.returncode == 0 else result.stderr
    except subprocess.CalledProcessError as e:
        logger.info(f"Command '{command}' failed with return code {e.returncode}")
        return e.stdout
    
    
@tool
def update_source_code(file_code, file_path):
    """Overwrite file at file_path with contents of file_code """
    file_path = file_path.replace("\\", "/")
    logger.info(f"Updating source code in {file_path}")
    try:
        with open(file_path, "w") as f:
            logger.info(f'Writing to {file_path}')
            f.write(file_code)
    except Exception as e:
        logger.error(f"Failed writing to {file_path}: {e}")  

@tool
def get_current_timestamp():
    """ Get the current date and time """
    return datetime.now()    

