# forge-tool

Setup:
1. Install Python requirements in python/requirements.txt
2. Make sure you have mvn and Java installed locally.
3. Setup AWS config params in AWS Systems Manager. You'll need a Github PAT in forge_tool_api_key and a SSH key for git in forge_tool_ssh_private_key.
4. Make sure you've exported your AWS credentials locally.
5. Run:\
    cd python\
    python upgrade_code.py
