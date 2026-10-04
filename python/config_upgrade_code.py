import os
import tempfile
import argparse

import settings
import workdir
from utils import get_logger, get_config
from agent import Claude
import streamlit as st
from git_utils import configure_github_token, get_github_api_url

logger = get_logger()

PARAMETER_NAMES = settings.SSM_PARAMETER_NAMES
MODEL_AWS_REGION = settings.BEDROCK_MODEL_REGION
PARAMETER_STORE_PREFIX = settings.PARAMETER_STORE_PREFIX

KB_DIRECTORY = settings.KNOWLEDGE_BASE_DIRECTORY

def config_upgrade_code(request):
    upgrade_details = request["upgrade_details"]
    repo_url = request["github_url"]
    repo_api_url = request["repo_api_url"]

    logger.info(f"Retrieving config")
    config = get_config(PARAMETER_STORE_PREFIX, PARAMETER_NAMES)
    # The PAT stays in process memory; tools read it from there so it never
    # has to pass through the model.
    configure_github_token(config["api_key"])

    # Select a model provider to perform the code generation
    work_root = settings.WORK_ROOT or None
    if work_root:
        os.makedirs(work_root, exist_ok=True)
    tmpdir = tempfile.mkdtemp(dir=work_root)
    # Every tool resolves model-supplied paths against this sandbox.
    workdir.set_working_dir(tmpdir)
    agent = Claude(model_aws_region=MODEL_AWS_REGION, working_dir=tmpdir)

    prompt = agent.create_prompt(upgrade_details, repo_api_url, repo_url, tmpdir)
    return agent, prompt


@st.cache_resource
def setup_upgrade_code():
    parser = argparse.ArgumentParser(
        description="Code upgrade tool."
    )
    parser.add_argument("--upgrade_details", required=False, type=str, help="Version(s) to upgrade to")
    parser.add_argument("--github_url", required=False, type=str, help="The GitHub Repo URL to upgrade")
    args = parser.parse_args()

    # CLI flag wins, then the .env default, then an interactive prompt.
    github_url = args.github_url or settings.DEFAULT_GITHUB_URL
    upgrade_details = args.upgrade_details or settings.DEFAULT_UPGRADE_DETAILS
    if (not github_url):
        github_url = input('Enter the Github URL: ')
    if (not upgrade_details):
        upgrade_details = input('What would you like to upgrade? (e.g. Spring boot 2.7.17 Java 17) ')

    repo_api_url = get_github_api_url(github_url)

    return config_upgrade_code({
        "github_url": github_url,
        "upgrade_details": upgrade_details,
        "repo_api_url": repo_api_url,
    })

if __name__ == "__main__":
    setup_upgrade_code()