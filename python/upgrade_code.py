import os
import tempfile
import time
import re
import argparse

from git_utils import GitHubProvider
from utils import get_logger, get_config
from bedrock import Claude
import logging

logger = get_logger()

PARAMETER_NAMES = [
    "ssh_private_key", "api_key"
]
MODEL_AWS_REGION = "us-east-1"
SSH_PRIVATE_KEY_FILENAME = "ssh_private_key"
PARAMETER_STORE_PREFIX = "forge_tool_"

def upgrade_code(request):
    upgrade_details = request["upgrade_details"]
    repo_url = request["github_url"]
    repo_api_url = request["repo_api_url"]

    logger.info(f"Retrieving config")
    config = get_config(PARAMETER_STORE_PREFIX, PARAMETER_NAMES)
    ssh_private_key = config["ssh_private_key"]
    api_key = config["api_key"]
    #api_key = os.getenv('GIT_API_KEY')

    # Select a model provider to perform the code generation
    tmpdir = tempfile.mkdtemp()
    agent = Claude(model_aws_region=MODEL_AWS_REGION, working_dir=tmpdir)

    # branch_name = f"upgrade-code-{round(time.time())}"
    # repo_name = repo_url.split("/")[-1]

    # Prepare SSH credentials for cloning the target repo
    ssh_private_key_path = os.path.join(tmpdir, "ssh_private_key")
    write_ssh_key(ssh_private_key, ssh_private_key_path)
    
    # Trigger the code generation 
    result = agent.upgrade_code(upgrade_details, repo_api_url, repo_url, api_key, ssh_private_key_path, tmpdir)

    while (True):
        human_message = input('\nUser enter feedback: ')
        result = agent.invoke(human_message, result)
    logger.info("Upgrade complete")

    return result


def write_ssh_key(value, file_path):
    """Retrieve git SSH private key from SSM and write to file."""
    logger.info(f"Writing SSH key to {file_path}")
    with open(file_path, "w") as f:
        f.write(value)
    os.chmod(file_path, int("600", base=8))


def get_github_api_url(github_url):
    pattern = "git@github.com:(.*).git"
    match = re.search(pattern, github_url)
    repo_name = match.group(1)
    return f'https://api.github.com/repos/{repo_name}'

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Code upgrade tool."
    )
    parser.add_argument("--upgrade_details", required=False, type=str, help="Version(s) to upgrade to")
    parser.add_argument("--github_url", required=False, type=str, help="The GitHub Repo URL to upgrade")
    args = parser.parse_args()

    # TODO: make this part of chat
    github_url = args.github_url
    upgrade_details = args.upgrade_details
    if (not github_url):
        github_url = input('Enter the Github URL: ')
    if (not upgrade_details):
        upgrade_details = input('What would you like to upgrade? (Spring boot 2.7.17 Java 17) ') or 'Spring boot 2.7.17 Java 17'

    repo_api_url = get_github_api_url(github_url)

    # git@github.com:lnealer/test_spring_upgrade_repo.git
    upgrade_code({
        "github_url": github_url,
        "upgrade_details": upgrade_details,
        "repo_api_url": repo_api_url,
    })
