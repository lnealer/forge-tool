import os
from abc import ABC, abstractmethod

import requests
from black import FileMode, format_str
from git import Repo
from langchain_core.tools import tool
import subprocess
import re
import stat

from utils import get_logger

logger = get_logger()

def remove_readonly(func, path, excinfo):
    os.chmod(path, stat.S_IWRITE)
    func(path)

@tool
def clone_repo(url, repo_dir, ssh_private_key_path):
    """Clone the target repo to the local file system."""
    
    try:
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
    except Exception as e:
        print("Failed cloning " + str(e))


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
    try:
        subprocess.run(["git", "checkout", "-b", branch_name], shell=True, capture_output=True, text=True)
        subprocess.run(["git", "add", "."], shell=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", commit_message], shell=True, capture_output=True, text=True)
        subprocess.run(["git", "push", "-u", "origin", branch_name],shell=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        logger.info(f"Create branch failed with return code {e.returncode}")
        return e.stdout
    

@tool
def git_commit(branch_name, repo_file_path, commit_message):
    """Commit changes and and push to the remote."""
    repo_file_path =repo_file_path.replace("\\", "/")
    logger.info(f"Writing commits to remote. file_path={repo_file_path}")
    repo_file_path =repo_file_path.replace("\\", "/")
    os.chdir(repo_file_path)
    subprocess.run(["git", "add", "."], shell=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", commit_message], shell=True, capture_output=True, text=True)
    subprocess.run(["git", "push", "-u", "origin", branch_name], shell=True, capture_output=True, text=True)

@tool
def create_pull_request(api_key, github_ssh_url, branch_name, pr_title, pr_description):
        """Create a pull request using the GitHub API."""
        repo_api_url = get_github_api_url(github_ssh_url)
        git_provider = GitHubProvider(api_key, repo_api_url)
        logger.info(f"Creating pull request. repo_api_url={repo_api_url}, branch_name={branch_name}")
        git_provider.create_pull_request(branch_name, pr_title, pr_description)


def get_github_api_url(github_url):
    """ Get the Github API Url for cloning """
    pattern1 = "git@github.com:(.*).git"
    pattern2 = "https://github.com/(.*).git"
    match = re.search(pattern1, github_url)
    if (not match):
        match = re.search(pattern2, github_url)

    repo_name = match.group(1)
    return f'https://api.github.com/repos/{repo_name}'

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