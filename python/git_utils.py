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

_GITHUB_TOKEN = ""

def configure_github_token(token):
    """Hold the PAT in process memory.

    The tools read it from here instead of the model passing it as an
    argument, so the token never enters the prompt, the conversation history
    or the Bedrock request body. That is also what lets the Bedrock guardrail
    block token patterns without blocking every request.
    """
    global _GITHUB_TOKEN
    _GITHUB_TOKEN = token or ""


def _github_token():
    if not _GITHUB_TOKEN:
        raise RuntimeError(
            "GitHub token not configured; configure_github_token() must run at startup."
        )
    return _GITHUB_TOKEN

def remove_readonly(func, path, excinfo):
    os.chmod(path, stat.S_IWRITE)
    func(path)

def clone_repo(url, repo_dir,  ssh_private_key_path):
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
        return str(e)

def format(content):
    """Format code."""
    return format_str(content, mode=FileMode())

def create_new_branch(branch_name, repo_file_path):
    """Create a new branch. Returns the current branch name"""
    repo_file_path =repo_file_path.replace("\\", "/")
    logger.info(f"Creating branch. file_path={repo_file_path}")
    repo_file_path =repo_file_path.replace("\\", "/")
    os.chdir(repo_file_path)
    current_branch = get_active_branch_name() 
    if (current_branch != "main"):
        logger.info("Remaining on branch "+ current_branch)
        return current_branch
    try:
        checkout = subprocess.run(["git", "checkout", "-b", branch_name], check=True, shell=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        logger.info(f"Create branch failed with return code {e.returncode} and error {e.stderr}, {e.stdout}\n")
        return e.stderr
    
def create_branch(branch_name, repo_file_path, commit_message):
    """Create a new branch. Returns the current branch name"""
    repo_file_path =repo_file_path.replace("\\", "/")
    logger.info(f"Creating branch. file_path={repo_file_path}")
    repo_file_path =repo_file_path.replace("\\", "/")
    os.chdir(repo_file_path)
    current_branch = get_active_branch_name() 
    if (current_branch != "main"):
        logger.info("Remaining on branch "+ current_branch)
        return current_branch
    try:
        checkout = subprocess.run(["git", "checkout", "-b", branch_name], check=True, shell=True, capture_output=True, text=True)
        add = subprocess.run(["git", "add", "."], shell=True, check=True, capture_output=True, text=True)
        commit = subprocess.run(["git", "commit", "-m", commit_message], check=True, shell=True, capture_output=True, text=True)
        push = subprocess.run(["git", "push", "-u", "origin", branch_name], check=True,shell=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        logger.info(f"Create branch failed with return code {e.returncode} and error {e.stderr}, {e.stdout}\n")
        return e.stderr
    
    current_branch=get_active_branch_name()
    logger.info("On branch "+ current_branch)
    return current_branch


def get_active_branch_name():
    """Retrieve the name of the current git branch. Returns branch_name."""
    logger.info("Getting active branch name...")
    try:
        # Runs 'git branch --show-current' and decodes the output string
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"], 
            stderr=subprocess.DEVNULL
        ).decode("utf-8").strip()
        
        return branch if branch else "Detached HEAD"
    except subprocess.CalledProcessError:
        return "Not a git repository (or git not installed)"

@tool
def git_commit(branch_name, repo_file_path, commit_message):
    """Commit changes and and push to the remote branch."""
    repo_file_path =repo_file_path.replace("\\", "/")
    logger.info(f"Writing commits to remote. file_path={repo_file_path}")
    repo_file_path =repo_file_path.replace("\\", "/")
    os.chdir(repo_file_path)
    try:
        subprocess.run(["git", "add", "."], shell=True, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", commit_message], check=True, shell=True, capture_output=True, text=True)
        subprocess.run(["git", "push", "-u", "origin", branch_name], check=True, shell=True, capture_output=True, text=True)
        return 0
    except subprocess.CalledProcessError as e:
        logger.info(f"Command failed with return code {e.returncode} and error {e.stderr}")
        return e.stderr

@tool
def create_pull_request(github_ssh_url, branch_name, pr_title, pr_description):
        """Create a pull request using the GitHub API."""
        repo_api_url = get_github_api_url(github_ssh_url)
        git_provider = GitHubProvider(repo_api_url)
        logger.info(f"Creating pull request. repo_api_url={repo_api_url}, branch_name={branch_name}")
        return git_provider.create_pull_request(branch_name, pr_title, pr_description)

@tool
def update_pull_request(github_ssh_url, branch_name, pr_description):
        """Update a pull request using the GitHub API."""
        repo_api_url = get_github_api_url(github_ssh_url)
        git_provider = GitHubProvider(repo_api_url)
        logger.info(f"Updating pull request. repo_api_url={repo_api_url}, branch_name={branch_name}")
        return git_provider.update_pull_request(branch_name, pr_description)

def get_github_api_url(github_url):
    """ Get the Github API Url for cloning """
    pattern1 = "git@github.com:(.*).git"
    pattern2 = "https://github.com/(.*).git"
    match = re.search(pattern1, github_url)
    if (not match):
        match = re.search(pattern2, github_url)

    repo_name = match.group(1)
    return f'https://api.github.com/repos/{repo_name}'


def get_repo_name(github_url):
    """ Get the Github API Url for cloning """
    pattern1 = "git@github.com:.*/(.*).git"
    pattern2 = "https://github.com/.*/(.*).git"
    match = re.search(pattern1, github_url)
    if (not match):
        match = re.search(pattern2, github_url)

    repo_name = match.group(1)
    return repo_name

# can be implemented for other git platforms (e.g. gitlab)
class GitProvider(ABC):
    @abstractmethod
    def create_pull_request(branch_name):
        pass


class GitHubProvider(GitProvider):
    """GitHub provider.

    Interacts with the GitHub API to perform git operations.
    """

    def __init__(self, repo_url):
        api_key=_github_token(api_key)
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
            return response.text or "Pull request created"
        else:
            logger.info(f"Failed to create pull request with error: {response.text}")
            return response.text
    
    def get_pull_request(self, branch):

        url = self.url
        logger.info(url)
        response = requests.get(url, headers=self.headers, timeout=30)

        if response.status_code != 200:
            logger.info(f"Failed to retrieve pull request with error: {response.text}")
            return response.text

        pulls = response.json()
        return next((x["url"] for x in pulls if x["head"]["ref"] == branch), None)
    

    def update_pull_request(self, branch, description):
        """Update a pull request description for given branch"""
        url = self.get_pull_request(branch)
        data = {
            "body": description,
        }

        response = requests.post(url, json=data, headers=self.headers, timeout=30)

        if response.status_code == 200:
            logger.info(f"Pull request updated ({response.json()['html_url']})")
            return response.text or "Pull request updated"
        else:
            logger.info(f"Failed to update pull request with error: {response.text}")
            return response.text