import os
import tempfile
import re
import argparse

from utils import get_logger, get_config
from agent import Reviewer,Writer,ChatBot,WRITER_PROMPT_TEMPLATE,REVIEWER_PROMPT_TEMPLATE,CHATBOT_PROMPT_TEMPLATE,create_prompt
import streamlit as st
from git_utils import get_github_api_url,clone_repo,create_new_branch,get_repo_name,configure_github_token
from langchain_core.prompts import ChatPromptTemplate, PromptTemplate
from langchain_core.messages import SystemMessage
from agent_graph import Graph
import time

logger = get_logger()

PARAMETER_NAMES = [
    "ssh_private_key", "api_key"
]
MODEL_AWS_REGION = "us-east-1"
SSH_PRIVATE_KEY_FILENAME = "ssh_private_key"
PARAMETER_STORE_PREFIX = "forge_tool_"

KB_DIRECTORY = "./knowledge-base"

def config_upgrade_code(request):
    upgrade_details = request["upgrade_details"]
    repo_url = request["github_url"]
    repo_api_url = request["repo_api_url"]

    logger.info(f"Retrieving config")
    config = get_config(PARAMETER_STORE_PREFIX, PARAMETER_NAMES)
    ssh_private_key = config["ssh_private_key"]
    api_key = config["api_key"]

    tmpdir = tempfile.mkdtemp()

    # Prepare SSH credentials for cloning the target repo
    ssh_private_key_path = os.path.join(tmpdir, "ssh_private_key")
    write_ssh_key(ssh_private_key, ssh_private_key_path)

    # create the prompts
    writer_prompt = create_prompt(WRITER_PROMPT_TEMPLATE, upgrade_details, repo_api_url, repo_url, ssh_private_key_path, tmpdir)
    reviewer_prompt = create_prompt(REVIEWER_PROMPT_TEMPLATE, upgrade_details, repo_api_url, repo_url, ssh_private_key_path, tmpdir)
    chat_prompt = create_prompt(CHATBOT_PROMPT_TEMPLATE, upgrade_details, repo_api_url, repo_url, ssh_private_key_path, tmpdir)

    reviewer = Reviewer(working_dir=tmpdir,prompt=reviewer_prompt)
    chatbot = ChatBot(working_dir=tmpdir,prompt=chat_prompt)
    writer = Writer(working_dir=tmpdir,prompt=writer_prompt)

    graph = initialize_graph(reviewer,writer)

    # configure api key
    configure_github_token(api_key)

    # clone repo and make branch
    branch_name = f"forge-tool-{int(time.time())}"
    path = os.path.join(tmpdir, get_repo_name(repo_url))
    clone_repo(repo_url, path,ssh_private_key_path)
    create_new_branch(branch_name, path)

    return writer, chatbot, reviewer, graph,branch_name,path

def initialize_graph(reviewer, writer):
    graph = Graph(reviewer, writer)
    app = graph.initialize_graph()
    return app

def write_ssh_key(value, file_path):
    """Retrieve git SSH private key from SSM and write to file."""
    logger.info(f"Writing SSH key to {file_path}")
    with open(file_path, "w") as f:
        f.write(value)
    os.chmod(file_path, int("600", base=8))

@st.cache_resource
def setup_upgrade_code():
    parser = argparse.ArgumentParser(
        description="Code upgrade tool."
    )
    parser.add_argument("--upgrade_details", required=False, type=str, help="Version(s) to upgrade to")
    parser.add_argument("--github_url", required=False, type=str, help="The GitHub Repo URL to upgrade")
    args = parser.parse_args()

    github_url = args.github_url
    upgrade_details = args.upgrade_details
    if (not github_url):
        github_url = input('Enter the Github URL: ') or 'git@github.com:lnealer/test_spring_upgrade_repo.git'
    if (not upgrade_details):
        upgrade_details = input('What would you like to upgrade? (Spring boot 2.7.17 Java 17) ') or 'Spring boot 2.7.17 Java 17'

    repo_api_url = get_github_api_url(github_url)

    # git@github.com:lnealer/test_spring_upgrade_repo.git
    return config_upgrade_code({
        "github_url": github_url,
        "upgrade_details": upgrade_details,
        "repo_api_url": repo_api_url,
    })

if __name__ == "__main__":
    setup_upgrade_code()