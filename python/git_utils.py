import os
from abc import ABC, abstractmethod

import requests
from black import FileMode, format_str
from git import Repo
from langchain_core.tools import tool
import subprocess


from utils import get_logger

logger = get_logger()

@tool
def clone_repo(url, repo_dir, ssh_private_key_path):
    """Clone the target repo to the local file system."""
    logger.info(f"Cloning repo {url} to {repo_dir}. ssh_private_key_path={ssh_private_key_path}")
    repo = Repo.clone_from(
        url,
        repo_dir,
        env={
            "GIT_SSH_COMMAND": f"ssh -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -i {ssh_private_key_path}"
        },
    )
    repo.config_writer().set_value("user", "name", "upgrade-code-bot").release()
    repo.config_writer().set_value("user", "email", "upgrade@code.bot").release()
    return repo


def update_source_code(files, repo_dir, format_code=True):
    """Overwrite files in target repo."""
    logger.info(f"Updating source code in {repo_dir}")
    for file in files:
        if format_code:
            contents = file.code
        with open(os.path.join(repo_dir, file.filename), "w") as f:
            logger.info(f'Writing to {file.filename}')
            try:
                f.write(contents)
            except Exception as e:
                logger.error(f"Failed writing to {file.filename}: {e}")


def format(content):
    """Format code."""
    return format_str(content, mode=FileMode())


@tool
def create_branch(branch_name, repo_file_path, commit_message):
    """Create a branch, commit changes, and push to the remote."""
    repo_file_path =repo_file_path.replace("\\", "/")
    logger.info(f"Creating branch. file_path={repo_file_path}")
    repo_file_path =repo_file_path.replace("\\", "/")
    os.chdir(repo_file_path)
    subprocess.run(["git", "checkout", "-q", "-b", branch_name])
    subprocess.run(["git", "add", "."])
    subprocess.run(["git", "commit", "-q", "-m", commit_message])
    subprocess.run(["git", "push", "-q", "-u", "origin", branch_name])

@tool
def git_commit(branch_name, repo_file_path, commit_message):
    """Commit changes and and push to the remote."""
    repo_file_path =repo_file_path.replace("\\", "/")
    logger.info(f"Creating branch. file_path={repo_file_path}")
    repo_file_path =repo_file_path.replace("\\", "/")
    os.chdir(repo_file_path)
    subprocess.run(["git", "add", "."])
    subprocess.run(["git", "commit", "-m", commit_message])
    subprocess.run(["git", "push", "-u", "origin", branch_name])

@tool
def create_pull_request(api_key, repo_api_url, branch_name, pr_title, pr_description):
        """Create a pull request using the GitHub API."""
        git_provider = GitHubProvider(api_key, repo_api_url)
        logger.info(f"Creating pull request. repo_api_url={repo_api_url}, branch_name={branch_name}")
        git_provider.create_pull_request(branch_name, pr_title, pr_description)


# can be implemented for other git platforms (e.g. gitlab)
class GitProvider(ABC):
    @abstractmethod
    def create_pull_request(branch_name):
        pass


class GitHubProvider(GitProvider):
    """GitHub provider.

    Interacts with the GitHub API to perform git operations.
    """

    def __init__(self, api_key, repo_url):
        self.url = f"{repo_url}/pulls"
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def create_pull_request(self, branch, title, description):
        """Create a new pull request for a target branch"""
        data = {
            "title": title,
            "body": description,
            "head": branch,
            "base": "main",
        }

        url = self.url
        logger.info(url)
        response = requests.post(url, json=data, headers=self.headers, timeout=30)

        if response.status_code == 201:
            logger.info(f"Pull request created ({response.json()['html_url']})")
        else:
            logger.info(f"Failed to create pull request with error: {response.text}")