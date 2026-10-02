from langchain_core.tools import tool
import subprocess
from datetime import datetime
from langchain_aws.retrievers import AmazonKnowledgeBasesRetriever
from langchain_core.tools import create_retriever_tool
import os
from utils import get_logger

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
    command = f'mvn -U clean test -f {code_dir}'
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
    command = f'mvn -U clean package -f {code_dir}'
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
