# Running forge on Windows

forge runs natively on Windows 10/11 with PowerShell. The Windows PC **only runs the
app**: AWS infrastructure (Terraform state, `deploy.sh`, writing the PAT to SSM) stays
on the macOS/Linux machine that created it. The PC reads everything it needs from
`.env` and SSM Parameter Store. [Docs index](README.md)

## 1. Install the toolchain

| Tool | Get it | Check |
| --- | --- | --- |
| Git for Windows | https://git-scm.com/download/win | `git --version` |
| Python 3.11+ (3.12 recommended) | https://www.python.org/downloads/ - tick **Add python.exe to PATH** | `py -3 --version` |
| JDK 21 | Eclipse Temurin 21 (https://adoptium.net) or Microsoft Build of OpenJDK 21 | `java -version` |
| Apache Maven 3.9+ | https://maven.apache.org/download.cgi - unzip, add `bin` to PATH | `mvn -v` |
| AWS CLI v2 | https://aws.amazon.com/cli/ | `aws --version` |

`winget` installs most of them in one go (PowerShell as your user):

```powershell
winget install --id Git.Git -e
winget install --id Python.Python.3.12 -e
winget install --id EclipseAdoptium.Temurin.21.JDK -e
winget install --id Amazon.AWSCLI -e
# Maven has no official winget package: download the binary zip, unzip to C:\tools\maven,
# and add C:\tools\maven\bin to your user PATH.
```

## 2. Allow long paths (once, as Administrator)

Maven trees under a migrated repository are deep; Windows stops at 260 characters by default.

```powershell
New-ItemProperty -Path HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force
git config --global core.longpaths true
```

forge also clones target repositories with `core.longpaths=true` and `core.autocrlf=true`
(Python writes CRLF on Windows; autocrlf keeps those from showing as whole-file diffs).

## 3. AWS credentials

Use the same IAM user as the Mac (`stocks-ai`, profile `dash-stocks`):

```powershell
aws configure --profile dash-stocks      # access key, secret key, region us-east-1
aws sts get-caller-identity --profile dash-stocks
```

## 4. Get the code and the configuration

```powershell
git clone https://github.com/lnealer/forge-tool.git C:\src\forge-tool
cd C:\src\forge-tool
git switch guardrails
```

`.env` is not in git. Copy it from the Mac (`~/forge-tool/.env`) to `C:\src\forge-tool\.env`,
then add or change these lines on the PC:

```ini
AWS_PROFILE=dash-stocks
# Short sandbox root: keeps deep Maven paths under Windows limits
WORK_ROOT=C:\forge-work
# Optional: let setup.ps1 pick the Windows Python launcher
PYTHON_BIN=
```

Everything else (knowledge base id, both guardrail ids, branches, models) stays as it is: the
AWS resources are shared, and the GitHub PAT is read from SSM at startup.

## 5. Install, check, run

```powershell
# If PowerShell blocks local scripts, run each command through:
#   powershell -ExecutionPolicy Bypass -File .\infra\setup.ps1 <command>
.\infra\setup.ps1 install      # .venv + python\requirements.txt, then check + config
.\infra\setup.ps1 check        # toolchain, AWS, ids, PAT presence, long paths, WORK_ROOT
.\infra\setup.ps1 run          # chat at http://localhost:8501, Ctrl+C stops it
```

Background mode and the lifecycle commands match `setup.sh`:

| Command | What it does |
| --- | --- |
| `start` | runs Streamlit hidden in the background; the forge log goes to `app.log` (Streamlit's banner to `app.stdout.log`); waits for the health check |
| `status` | pid, uptime, health, connected browsers, run activity |
| `stop [-Force]` | refuses while a browser is connected or `app.log` changed in the last 5 minutes |
| `restart [-Force]` | stop + start |
| `config` | the effective configuration, each value tagged `shell`, `.env` or `default` |

## 6. Prove the port works

The offline suites need no AWS access (Bedrock, GitHub and Maven are stubbed):

```powershell
.\.venv\Scripts\python.exe tests\run_all.py
```

All seven suites should pass, including `test_windows` (Maven found as `mvn.cmd`, UTF-8 git
output, clone options).

## What stays on the Mac

| Task | Where |
| --- | --- |
| `infra/deploy.sh` (knowledge base, guardrails, IAM policy; Terraform state is local there) | Mac |
| `infra/put_ssm_parameters.sh` (storing or rotating the GitHub PAT) | Mac |
| `./infra/deploy.sh --sync-only` after adding or editing guideline packs | Mac |

Both machines use the same AWS account, so Bedrock's daily token limit is shared: a run on the
PC and a run on the Mac on the same day count against the same cap.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `python3` opens the Microsoft Store | leave `PYTHON_BIN` empty; `setup.ps1` uses the `py` launcher |
| `No JDK 21 found` | install Temurin 21, or set `JAVA_HOME` to the JDK folder |
| `Filename too long` from git or Maven | step 2 (long paths) and a short `WORK_ROOT` |
| `UnicodeDecodeError` in logs | start through `setup.ps1` (it sets `PYTHONUTF8=1`) |
| `MISSING valid AWS credentials` | `AWS_PROFILE` in `.env` must match the profile from step 3 |
