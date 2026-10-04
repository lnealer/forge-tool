# forge-tool

An agentic tool that migrates the tech stack of J2EE applications. Point it at a
Java repository and say what you want upgraded; it clones the repo, consults
your company coding guidelines from a Bedrock knowledge base, rewrites the
relevant code, runs the Maven test suite, and opens a pull request — then stays
in a chat session so you can review and ask for changes.

Supported targets today: Java, Spring, Spring Boot and Struts.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the pieces fit together:
components, the turn model, tool contracts, trust boundaries and infrastructure.
The [docs index](docs/README.md) also has one page per stage of a run (prepare, discover,
plan, migrate, review and PR) and [how files are picked for migration](docs/file-selection.md).

Two safety layers sit around the agent: **guardrails** (a local secret scanner that
refuses to write or commit keys, plus an Amazon Bedrock Guardrail that masks PII and
blocks secret patterns in model traffic) and a **reviewer model** (Amazon Nova Pro)
that scores every migrated file and sends anything below the threshold to you for
manual review in the chat before a PR is opened.

## How it works

```
Streamlit chat (python/chat.py)
        │
        ▼
config_upgrade_code.py ── SSM Parameter Store ──► GitHub PAT (held in process memory,
        │                                          never in the prompt), KB id, guardrail id
        ▼
  agent.py (Claude on Bedrock + LangGraph agent)  ◄─ Bedrock Guardrail on every call
        │
        ├── code_upgrade_knowledge_base ─► Bedrock KB ─► S3 Vectors  (optional)
        ├── read_file / list_directory             (scoped to a temp dir)
        ├── write_file                             (guarded: refuses secret material)
        ├── scan_for_secrets                       (SSH/PEM keys, AES keys, AWS keys, tokens, passwords)
        ├── detect_tech_stack / list_guideline_packs / propose_migration_plan  (discover → plan → approve)
        ├── list_migration_files / check_pack_acceptance  (file inventory per approved pack; leftovers as file:line)
        ├── review_migrated_files ─► Nova Pro      (scores each file; low scores -> manual review in chat)
        ├── run_maven_test / run_maven_install     (mvn clean test | clean install -DskipTests, per module)
        └── clone_repo / create_branch / git_commit / git_status / git_restore_file / create_pull_request
                              └── commit gate: staged diff scanned, secrets block the push
```

| File | Role |
| --- | --- |
| [python/chat.py](python/chat.py) | Streamlit UI and the chat loop |
| [python/config_upgrade_code.py](python/config_upgrade_code.py) | Reads the PAT from SSM into process memory, builds the agent and its prompt |
| [python/agent.py](python/agent.py) | Bedrock model, the prompt template, and the agent's tools |
| [python/git_utils.py](python/git_utils.py) | Clone, branch, commit, push, and the GitHub pull request API |
| [python/utils.py](python/utils.py) | Logging, Parameter Store access, guardrail/KB id resolution |
| [python/guardrails.py](python/guardrails.py) | Secret patterns, scanner, redaction, manual-review queue |
| [python/reviewer.py](python/reviewer.py) | Nova Pro reviewer, scoring rubric, `review_migrated_files` tool |
| [python/settings.py](python/settings.py) | All configuration, loaded from `.env` |
| [python/ui_events.py](python/ui_events.py) | Secret-guardrail events and the proposed plan, raised inside tools, shown in the chat |
| [python/techstack.py](python/techstack.py) | Deterministic tech-stack detection (poms, configs, imports) |
| [python/workdir.py](python/workdir.py) | The per-run sandbox every tool path is anchored to |
| [infra/](infra/) | Setup scripts and the AWS infrastructure (Terraform) |

```
infra/
  setup.sh                      local env setup, readiness check, Streamlit launcher
  put_ssm_parameters.sh         GitHub PAT -> SSM SecureString
  deploy.sh                     wraps terraform apply/destroy + docs upload + ingestion
  lib/env.sh                    shared .env loader
  terraform/
    versions.tf                 provider pins (aws ~> 6.67, time), optional S3 backend
    variables.tf                every input, fed from .env as TF_VAR_<name>
    main.tf                     wires the three modules, derives names + IAM principal
    outputs.tf                  ids deploy.sh writes back to .env
    .terraform.lock.hcl         provider hashes (committed); terraform.tfstate is git-ignored
    modules/
      knowledge_base/           S3 docs bucket, S3 Vectors bucket + index, role, KB, data source, SSM id
      guardrail/                Bedrock Guardrails: model guardrail (PII masking + secret blocking) and the PII scan guardrail, versions, SSM ids
      permissions/              IAM managed policy with the app's runtime permissions
```

## Prerequisites

- Python 3.10+
- Java (JDK 17 or 21) and Maven on `PATH` — the agent runs `mvn` against the target repo
- Terraform >= 1.6 (`brew install hashicorp/tap/terraform`)
- AWS CLI v2 and credentials (see step 1), in an account with Bedrock model access
  enabled for **Claude Haiku 4.5**, **Amazon Nova Pro** (the reviewer) and
  **Titan Embed v2** (the knowledge base)
- A GitHub personal access token (`repo` scope, or fine-grained with Contents +
  Pull requests read/write) for the repositories you want to upgrade. Clone, push
  and PR all use the PAT over HTTPS; no SSH key is needed

## Setup

### 1. Configuration

Every tunable value lives in `.env`. Copy the template and edit as needed:

```bash
cp .env.example .env
```

`.env` is git-ignored; `.env.example` is the committed reference.

**AWS credentials** go in the same file. Because the app and every infra script
read `.env`, credentials set there work regardless of which terminal exported them:

```bash
aws configure --profile forge      # once
# then in .env:
AWS_PROFILE=forge
```

Static `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` work too.
Leave them all blank to use the default credential chain. Confirm with
`./infra/setup.sh check`.

An exported environment variable always wins over the `.env` file, in the shell
scripts (`infra/lib/env.sh`) and in the app (`load_dotenv(override=False)`)
alike, so CI and containers can override anything without editing files:

```bash
AWS_REGION=us-east-1 ./infra/deploy.sh      # deploys to eu-west-1; .env untouched
```

When the effective region differs from the one in `.env` like this, `deploy.sh`
deliberately does **not** write the resulting ids into `.env` (they are
region-specific); the app resolves them from SSM in its own region instead.

One exception worth knowing: `JAVA_VERSION` is a pin, so it takes precedence
over a `JAVA_HOME` inherited from your shell. `./infra/setup.sh check` prints
which source won.

### 2. Install dependencies

```bash
./infra/setup.sh install
```

This creates `.venv`, installs `python/requirements.txt`, resolves `JAVA_HOME`,
puts `python/` on `PYTHONPATH`, and then prints a readiness check.

To export the same paths into your current shell without installing anything:

```bash
source infra/setup.sh
```

### 3. Store the GitHub PAT in SSM Parameter Store

```bash
./infra/put_ssm_parameters.sh
```

Writes `forge_tool_api_key` as a `SecureString` (prefix from `PARAMETER_STORE_PREFIX`).
The token is read from a hidden prompt (or `GITHUB_PAT` if exported) and handed to
the CLI through a `0600` temp file, so it never appears in `argv` or shell history.
At runtime the PAT is held in process memory and the git/GitHub tools read it from
there — it is never placed in the prompt, the conversation history or any Bedrock
request. Re-run the script at any time to rotate it; `--show` lists what exists.

### 4. Create the AWS infrastructure

```bash
./infra/deploy.sh --sync
```

`deploy.sh` is a thin wrapper around Terraform ([infra/terraform/](infra/terraform/)):
it exports every infra setting in `.env` as `TF_VAR_<lowercase name>`, runs
`terraform init` + `apply -auto-approve`, reads the outputs, writes the ids back to
`.env`, and (with `--sync`) uploads `knowledge-base/` and starts an ingestion job.
Three modules, each a part you can manage separately:

| Module | What it creates |
| --- | --- |
| `knowledge_base` | S3 Vectors knowledge base (below) |
| `guardrail` | Bedrock Guardrail, a published version, SSM ids |
| `permissions` | IAM managed policy, attached to the deploying identity |

Two different ways to leave a part out, and they mean different things:

- **Target only some parts this run** — `--kb-only`, `--guardrail-only`,
  `--permissions-only`, or `--no-kb` / `--no-guardrail` / `--no-permissions`. These
  pass `-target` to Terraform. One subtlety: the IAM policy is scoped to the
  guardrail and knowledge base ids, so Terraform treats those two modules as
  *dependencies* of `permissions` and includes them whenever `permissions` is
  targeted. `--kb-only`, `--guardrail-only` and `--no-permissions` touch exactly what
  they name; `--permissions-only`, `--no-kb` and `--no-guardrail` will also create a
  missing guardrail/KB or apply a pending config change to it. Existing, unchanged
  parts are left as they are. `deploy.sh` prints a warning in that case.
- **Run without a part at all** — `CREATE_KNOWLEDGE_BASE=false` (or
  `CREATE_GUARDRAIL`, `CREATE_PERMISSIONS`) in `.env`. The module's `count` goes to
  0: it is not created, and an existing one is destroyed on the next apply. This is
  the right way to deploy without a knowledge base.

`--plan` shows exactly what a run would change without changing anything. Only the
knowledge base stores anything; the guardrail is billed per request and the policy
is free.

You can also drive Terraform directly — `deploy.sh` adds nothing but the
`TF_VAR_*` export, the `.env` write-back and the docs upload. Every variable has
a default matching `.env.example`, so you only need to export the ones you changed:

```bash
TF_VAR_kb_embedding_dimensions=512 terraform -chdir=infra/terraform plan
terraform -chdir=infra/terraform output              # the ids deploy.sh writes to .env
```

State is a local `terraform.tfstate` next to the config (git-ignored; the provider
lock file *is* committed). To share state across machines, uncomment the `backend
"s3"` block in [versions.tf](infra/terraform/versions.tf) and run
`terraform -chdir=infra/terraform init -migrate-state`.

#### Knowledge base

[modules/knowledge_base](infra/terraform/modules/knowledge_base/main.tf) creates
everything the agent needs to look up your coding guidelines:

| Resource | Purpose |
| --- | --- |
| `aws_s3_bucket` (+ versioning, SSE, public-access block) | holds the guideline documents |
| `aws_s3vectors_vector_bucket` | holds the embeddings |
| `aws_s3vectors_index` | the vector index (`float32`, cosine, 1024 dims) |
| `aws_iam_role` + inline policy | what Bedrock assumes to read the docs and write vectors |
| `aws_bedrockagent_knowledge_base` | `storage_configuration.type = "S3_VECTORS"` |
| `aws_bedrockagent_data_source` | crawls `guidelines/` with fixed-size chunking |
| `aws_ssm_parameter` | publishes the knowledge base id to the app |

With `--sync` it then uploads `knowledge-base/` to S3 and starts an ingestion
job. On success `KNOWLEDGE_BASE_ID` is written back into `.env`; the app reads
`.env` first and falls back to the SSM parameter, so a fresh checkout on another
machine needs no edit.

**Why S3 Vectors.** The embeddings are billed per request and per GB stored,
with no provisioned capacity. An OpenSearch Serverless vector collection would
instead bill a minimum OCU around the clock whether or not you query it, and
vector collections cannot share OCUs with other collections. S3 Vectors indexes
are a first-class Terraform resource, so there is no out-of-band index bootstrap
step — the whole knowledge base is one `terraform apply`.

`KB_EMBEDDING_DIMENSIONS` is applied to both the index and the knowledge base
and must be a size the embedding model supports (Titan v2: 1024, 512 or 256).
Changing it later replaces the index and the knowledge base, so re-run `--sync` to
re-ingest afterwards.

Other commands:

```bash
./infra/deploy.sh              # apply, no upload
./infra/deploy.sh --plan       # show the plan only
./infra/deploy.sh --sync-only  # re-upload documents and re-ingest
./infra/deploy.sh --delete     # terraform destroy (asks you to type the project name; --yes skips)
```

`--delete` removes **everything** the modules created, including the documents
bucket and the vector bucket (both use `force_destroy`). That is deliberate: the
docs are a copy of `knowledge-base/` and the vectors are regenerated by `--sync`,
while retaining them only ever made the next create collide on the same names.
The PAT in SSM is never touched — it is not managed by Terraform.

**The knowledge base is optional.** Set `CREATE_KNOWLEDGE_BASE=false` in `.env`
and deploy; `load_kb_tool()` returns `None`, so the agent starts without the
`code_upgrade_knowledge_base` tool (and the IAM policy is scoped to
`knowledge-base/*` until one exists). That is the cheapest first run: prove the
clone/upgrade/test/PR loop, then flip it to `true` and run
`./infra/deploy.sh --sync`.

#### Guardrail

[modules/guardrail](infra/terraform/modules/guardrail/main.tf) creates an `aws_bedrock_guardrail`
with a **sensitive information policy** and publishes a numbered version plus the
SSM parameters `forge_tool_guardrail_id` / `forge_tool_guardrail_version`. The app
resolves those (or `GUARDRAIL_ID` / `GUARDRAIL_VERSION` in `.env`) and attaches the
guardrail to both the Claude agent and the Nova Pro reviewer on every call.

| Filter | Entities / patterns | Input | Output |
| --- | --- | --- | --- |
| PII | `AWS_ACCESS_KEY`, `AWS_SECRET_KEY`, `PASSWORD`, card number, SSN, bank account, IBAN, PIN | not masked (verified: the configured action applies to output only) | `GUARDRAIL_PII_ACTION` (default ANONYMIZE) |
| Regex | SSH/PEM/PGP private key, GitHub token, AES/symmetric key assignment, Slack token, JWT | detect only | **BLOCK** |

**Personal data in the repository (Amazon Bedrock scan).** The model guardrail only sees
model traffic, so a second guardrail, `<GUARDRAIL_NAME>-pii-scan`, scans the repository
itself. `scan_for_secrets` sends the data-bearing files (`PII_SCAN_GLOBS`: SQL, CSV, JSON,
properties, YAML, text, `src/*/resources`, test Java) through the Bedrock ApplyGuardrail API
in chunks and reports what it detects: names, emails, phone numbers, addresses, SSNs, tax and
passport numbers, driver IDs, card numbers/CVV/expiry, bank and routing numbers, IBAN, SWIFT,
UK NI and Canadian SIN numbers, PINs. Findings are shown by type and `file:line` in the chat
and in the PR's verification section; **values are never shown or kept**, and nothing is
changed (report only). `IP_ADDRESS`, `AGE`, `URL` and `USERNAME` are left out because code
and tests are full of them. Cost: about $0.10 per 1,000 text units (1,000 characters); the ams
repository is 420 units (~$0.04) and takes about a minute. `PII_SCAN_MAX_CHARS` caps a scan,
identical chunks are not re-sent, and `PII_SCAN_ENABLED=false` turns it off. On ams it reports
199 items in 27 files, almost all demo seed SQL and test fixtures.

Why that split: a legacy file containing a key must not abort the whole run when
the agent *reads* it (input), but the model must never *echo* secret material into
the chat (output). PII is limited to credentials and financial identifiers on
purpose; `EMAIL`, `NAME`, `URL` and `IP_ADDRESS` appear legitimately all over Java
projects and masking them would corrupt code. Extend `pii_entity_types` in the
module's variables to add more. The version is replaced whenever the guardrail
changes (`replace_triggered_by`), so the app always runs the current config. The
calling identity needs `bedrock:ApplyGuardrail` — the permissions module grants it.

#### Runtime permissions

[modules/permissions](infra/terraform/modules/permissions/main.tf) creates one
`aws_iam_policy` with exactly what the app needs and nothing more:
`bedrock:InvokeModel` and `InvokeModelWithResponseStream` (these also authorize
the Converse API) on the primary and reviewer inference profiles
**and** their underlying foundation models in every region (cross-region profiles
route there), `bedrock:ApplyGuardrail`, `bedrock:Retrieve`, `ssm:GetParameter(s)`
on the `forge_tool_*` prefix, and `kms:Decrypt` via SSM. `APP_IAM_PRINCIPAL=auto`
attaches it to whoever runs `deploy.sh`; use `user:<name>`, `role:<name>` or `none`.
Because all three are one Terraform graph, the policy is scoped to the exact
guardrail ARN and knowledge base id in the same apply — no second pass needed.

### 5. Verify

```bash
./infra/setup.sh check
```

Checks the toolchain (`python3`, `mvn`, `java`, `git`, `aws`, `terraform`), AWS
credentials, whether the knowledge base and guardrail ids resolve, which JDK was
picked and why, and that the PAT is in SSM.

## Running on Windows

forge runs natively on Windows with PowerShell: `.\infra\setup.ps1 install | check | run |
start | stop | status | config` mirrors `setup.sh`. The PC runs the app only; Terraform and SSM
writes stay on the macOS/Linux machine. Step-by-step setup: [docs/WINDOWS.md](docs/WINDOWS.md).

## Running

```bash
./infra/setup.sh run
```

Starts Streamlit in the foreground (output also written to `app.log`) on `http://localhost:8501` (configurable via
`STREAMLIT_SERVER_ADDRESS` / `STREAMLIT_SERVER_PORT`); Ctrl-C stops it. To run it
detached instead, with the output in `app.log`:

```bash
./infra/setup.sh start               # detached; waits until /_stcore/health answers
./infra/setup.sh status              # pid, uptime, health, connected browsers, recent activity
./infra/setup.sh stop                # refuses if a browser is connected or a run looks active
./infra/setup.sh stop --force        # stop anyway
./infra/setup.sh restart [--force]   # stop + start
./infra/setup.sh config              # print the effective configuration without starting anything
```

`install`, `run`, `start`, `restart` and `check` end by printing the **effective
configuration**: the paths exported for the process (`JAVA_HOME` and which JDK it
resolved to, `PYTHONPATH`, the venv, Maven options), the AWS region and credential
source, the model ids, knowledge base and guardrail ids (or the SSM parameter they will
be resolved from), the git branches and run defaults, the agent limits and the server
URL and log file. Each value is tagged with where it came from: `shell` (exported before
the script ran, which wins), `.env`, or `default`. Credentials are shown as `set` /
`unset`, never printed. `./infra/setup.sh config` prints the same summary on its own.

`stop` and `restart` guard against the easy mistake: a restart kills any upgrade in
progress (the clone and edits live in the server's temp dir) and resets the chat, so
they refuse while a browser session is connected or `app.log` has changed in the last
`RUN_ACTIVE_MINUTES` (default 5) unless you pass `--force`. `start` rotates the previous
`app.log` to `app.log.1` rather than truncating it. The same commands exist as
`make start|stop|restart|status` (`make restart FORCE=1`).

**When do you need a restart?** For a change to `python/chat.py` alone, Streamlit
offers **Rerun** in the browser and keeps your chat history. A restart is needed when
other modules under `python/` or `.env` change (the agent is cached for the process
lifetime).

With no arguments `run`/`start` use `DEFAULT_GITHUB_URL` and `DEFAULT_UPGRADE_DETAILS`
from `.env`; override per run:

```bash
./infra/setup.sh run --github_url https://github.com/org/repo.git --upgrade_details "Java 21"
```

Or drive Streamlit directly, if you have already activated the virtualenv:

```bash
streamlit run python/chat.py -- --github_url https://github.com/org/repo.git --upgrade_details "Java 21"
```

The agent opens with a summary of its instructions and waits for your
confirmation before touching anything. A run then goes: `clone_repo` at
`GIT_SOURCE_BRANCH` → `scan_for_secrets` → `detect_tech_stack` → `list_guideline_packs`
+ knowledge base lookups → `propose_migration_plan` (**you approve**) → edits →
`run_maven_install` (parent/shared modules) → `run_maven_test` → `review_migrated_files` → (manual review in the chat if any
file scored low) → `create_branch` → `create_pull_request` against
`GIT_BASE_BRANCH`. Ask for changes in the chat and it pushes follow-up commits to
the same branch.

## Discovery and migration plan

The agent does not take the goal literally. Before editing anything it works out
what the repository actually is and proposes what to migrate:

1. **Detect** — `detect_tech_stack` ([python/techstack.py](python/techstack.py)) parses
   the build deterministically: Maven reactors and modules, declared Java versions,
   every framework with its version and the modules using it (Spring/Boot/Security,
   Struts, Hibernate/JPA, iBatis/MyBatis, JUnit, JSF, JAX-RS, JMS, EJB, Servlet/JSP/JSTL,
   Log4j), legacy libraries, `javax` vs `jakarta` import counts per module, and the app
   server (Liberty features, Tomcat config, `web.xml`, Docker images). No model guesses:
   the BOM can be ahead of the sources and the server config behind both, and this shows
   all three.
2. **Map to the packs** — each pack in `knowledge-base/` carries YAML front matter with
   its own `detect` rules (`dependency`, `dependency_lt`, `import_prefix`, `file_glob`,
   `content_match`, `property_lt`, `xml_element`, and `decision_equals` gates such as
   `container=tomcat`), a `tier`, `depends_on` ordering and a `status`. The detector
   evaluates those rules against what it found and returns `packs.applicable` — matched
   packs **with evidence, in dependency order** — plus the packs gated on a decision.
   `status: detect-only` packs have no transform guidance yet and are planned as
   manual items. The agent then reads each applicable pack's `## transform` section
   through the knowledge base (`list_guideline_packs` shows titles and tiers).
3. **Propose** — `propose_migration_plan` records one item per component with a pack:
   current → target, pack, scope (modules/files), risk, notes. Items needed for your goal
   (`DEFAULT_UPGRADE_DETAILS`, e.g. "Java 21") are **in scope**; other candidates are listed
   as **optional**. The plan appears in the chat as a table with **Approve plan** and
   **Approve incl. optional items**. Describe a change in the chat to have it re-proposed.
4. **Migrate** — only approved in-scope items, following the packs; then tests, review, PR.
   The PR description carries the stack summary and the plan table.

**Which files get migrated.** Detection decides *which packs* apply. `list_migration_files`
then evaluates the approved packs' `applies_to` selectors into a deterministic inventory,
`file → [packs in dependency order]`, marked MUST CHANGE (a pack's acceptance pattern still
hits it) or VERIFY ONLY. The agent edits each MUST CHANGE file **once**, applying all its
packs in that order (javax rename before framework before view/test changes).
`check_pack_acceptance` then evaluates each pack's `acceptance` rules and reports leftovers
as `file:line`; anything left goes into a "Detected but not migrated" section of the PR. The
pack set is frozen when you approve the plan, so a pack that stops matching detection
mid-migration is still checked. Details and diagrams:
[docs/file-selection.md](docs/file-selection.md).

`MIGRATION_PLAN_APPROVAL=false` makes the agent inform and proceed without waiting.

## Watching a run

- **Activity feed (in the chat).** While the agent works, a "Working…" status box
  under your message lists each tool call as the model issues it (▶
  `run_maven_test {"code_dir": "repo/ams-common"}`), each result as it comes back
  (◀ `… SUCCESS`), and the model's narration between calls. It collapses to "Done"
  when the final answer arrives, or "Failed" with the error.
- **Secret guardrail events (in the chat).** Whenever the guardrail acts — a
  `write_file` refused for containing secret material, a commit blocked because the
  staged diff adds one, or `scan_for_secrets` finding secrets already in the
  repository — the file and the redacted findings appear as a red block in the
  activity feed the moment it happens, and in a persistent "Secret guardrail" panel
  above the input until you dismiss it.
- **`app.log`** (when started with `./infra/setup.sh start`) carries the same
  `→ tool` / `← result` lines plus git, Maven, reviewer and guardrail output;
  `./infra/setup.sh status` summarizes recent activity from it.

Granularity is one graph step: a tool call is announced when the model emits it and
its result appears when the tool returns, so a long `mvn test` shows as ▶ … then ◀
minutes later.

## Tests, branches and long turns

- **Baseline first.** Before changing anything the agent runs `run_maven_install` on the
  parent/BOM poms and on each reactor in build order, then `run_maven_test(baseline=true)`
  on each reactor. Every later test run is compared with that baseline: **new failures are
  regressions and get fixed; pre-existing failures are listed in the PR, not chased**. A
  reactor whose build never reaches the tests (broken pom, compile error) is recorded as
  such, so nothing in it is later mistaken for "pre-existing".
- **Structured results.** A Maven run comes back as a parsed summary — test counts, failing
  tests (`Class.method`), compile and pom errors, per-module reactor status, the baseline
  diff — followed by a short log tail (`MAVEN_OUTPUT_MAX_CHARS`), so a 7-module build does
  not consume the agent's context.
- **Reactors, not modules; one build at a time.** Tests run per reactor (the pom with
  `<modules>`), which builds every module in order. Maven runs are serialized inside the
  tool: parallel builds share `~/.m2` and file-based test databases (H2 allows one JVM per
  file) and fail for reasons that have nothing to do with the code.
- **Branch names come from the tool.** `create_branch` names the branch
  `forge-upgrade-<timestamp>` itself, reuses the current `forge-upgrade-*` branch on
  repeated calls, and stages and scans *before* creating it — so a blocked commit never
  leaves a half-made branch and "already exists" cannot happen. `git_status` shows the
  branch and uncommitted changes at any point.
- **Step limit = pause, not failure.** A turn is capped at `AGENT_RECURSION_LIMIT` graph
  steps (default 400; a multi-module migration takes a few hundred). Hitting it keeps all
  work in the working copy, shows the agent's last progress note, and offers a
  **Continue** button; the agent resumes via `git_status`.
- **Context compaction.** Past `AGENT_SUMMARY_TRIGGER_TOKENS` (default 120,000) the older
  history is summarized and the last `AGENT_SUMMARY_KEEP_MESSAGES` stay verbatim, so the
  model does not end a turn early "due to token limits".
- **Unfinished work is detected by code.** After every turn the chat runs the approved
  packs' acceptance checks; if any pack is unfinished and no decision is pending, it lists
  the packs and leftover counts and offers **Continue**.

## Guardrails

Secrets are stopped at four points, in order of distance from the code:

1. **PAT never enters the model.** `configure_github_token()` holds it in process
   memory; `clone_repo` and `create_pull_request` read it from there. It is not in
   the prompt, the history, or any Bedrock request body.
2. **`write_file` is guarded.** Content that matches a secret pattern is refused
   with the redacted findings, so the agent can fix it (read from env instead).
3. **Commit gate.** `create_branch` and `git_commit` stage, scan the staged diff's
   *added* lines, and on a hit unstage and refuse — nothing with a key is pushed.
   Only added lines are scanned so a key already in the repo's history does not
   block every commit; `scan_for_secrets` reports those instead.
4. **Bedrock Guardrail** on every model call (above), plus the chat redacts any
   secret pattern before an assistant message is rendered or stored.

Every local block (1–3) is also shown in the chat as it happens — see *Watching a run*.

**Secrets that are already in the repository** (found by `scan_for_secrets` right after
the clone) have three outcomes, and none of them copies the value anywhere:

- **Reported** — file, line and kind, value redacted, in the chat and in the PR description.
- **Protected** — the agent is told never to copy, print or move them; untouched lines are
  left alone, but any diff that *adds* a secret line is refused by the commit gate.
- **Placeholdered** — when a migration must rewrite a file that contains one (the
  Liberty → Tomcat pack rewrites `server.xml`, for instance), the value becomes an
  environment placeholder such as `${DB_PASSWORD}` and the PR gets a *Configuration
  required* section listing every placeholder introduced, with a note to rotate the originals.

**Secrets are always externalized.** When the scan finds any, the plan includes the pack
**externalize-secrets** as in scope, every time: each literal becomes an environment lookup
(`System.getenv("AES_KEY")` with a fail-fast check, `@Value("${AES_KEY}")` in Spring beans,
`HexFormat.of().parseHex(...)` for byte-array keys, `${env.…}` in `server.xml`), every
variable is listed under *Configuration required*, and `check_pack_acceptance` fails until no
literal is left anywhere in the repository.

**Public keys are your call.** Public keys are not secret, so they never block a write or a
commit. When the scan finds one, the agent stops and the chat shows **🧹 Scrub public keys**
(adds the pack **externalize-public-keys**, which moves them to configuration) and
**▶ Continue (keep them)** (they are listed in the PR instead).

Patterns (see `SECRET_PATTERNS` in [python/guardrails.py](python/guardrails.py)), each with a
severity:

| Severity | Patterns |
| --- | --- |
| secret (blocked, always externalized) | SSH/PEM/PGP private keys; AWS access key ids and secret keys; GitHub and Slack tokens; JWTs; AES / HMAC / signing / secret key assignments; `new SecretKeySpec("literal"…)`; byte-array keys and IVs (`byte[] key = {0x01, …}`); `*KEY` constants with a base64/hex literal; passwords, including prefixed constants like `GATEWAY_PASSWORD`; API keys and tokens, including `SETTLEMENT_API_KEY`; Liberty `<variable name="…password" defaultValue="literal"/>` |
| public (reported; you decide) | PEM `-----BEGIN PUBLIC KEY-----`; base64 X.509 public keys (RSA `MIIBIjAN…`, `MIIC…`, `MIGfMA0…`, EC P-256 `MFkwEwYH…`) |

Each needs a known token prefix or an assignment context, so ordinary identifiers like
`PrivateKey key = …` or message keys like `"error.validation.required"` do not trip them. Suppress a known false positive with
`SECRET_SCAN_ALLOWLIST` (`;`-separated regexes); turn the scanner off with
`SECRET_SCAN_ENABLED=false`.

## Pull request gate

`create_pull_request` opens nothing until a gate passes, judged from tool results, not from
the model's own summary:

1. the working tree is clean and HEAD is pushed;
2. every reactor's latest `run_maven_test` ran on the **current** commit, compiled, and has no
   new failures against the baseline (a failure is "pre-existing" only if the baseline says so);
3. every approved pack is clean, unless **you** accepted its leftovers: the agent cannot descope
   approved work, and a refusal shows **▶ Finish** / **Accept as not migrated** buttons per pack;
4. tests keep their meaning: `check_test_parity` finds no test file that lost test methods,
   assertions or expected values (unless you approved that file in the review panel).

A refusal lists every open item and the agent must fix them. The PR description then gets a
generated **Verification** section: per-reactor test results before and after, pack status
with leftovers and manual checks, test-parity findings, and the environment variables the new
code reads. `PR_GATE=block` (default) refuses; `draft` opens a GitHub draft with the open items
on top; `off` disables the gate.

## Reviewer model

After the upgrade compiles and tests pass, the agent calls `review_migrated_files`.
For every changed file (diffed against `origin/<GIT_SOURCE_BRANCH>`), Amazon Nova
Pro ([python/reviewer.py](python/reviewer.py)) is given the upgrade goal, the transform guidance of the
approved packs that select the file, the whole file after the migration and the unified diff, with a 0–10 rubric, and returns a score, summary
and issues as JSON. The rubric is evidence-only: every issue must cite a line or symbol in
the file, speculative issues are not allowed, and an import-only change counts as complete
when nothing else in the file uses the old API. Repo-wide completeness is the job of
`check_pack_acceptance`, not the reviewer.

- Files scoring **≥ `REVIEW_SCORE_THRESHOLD`** (default 7) pass.
- Files **below** it are queued for **manual review in the chat**: a warning panel
  appears above the input with each file's score, the reviewer's issues, the
  redacted diff, and three buttons. The agent is instructed to stop and wait for
  your decision before pushing or opening the PR:
  - **Approve** — keep the file as the agent wrote it (it is not raised again even if
    a later re-review scores it low);
  - **Deny** — the agent restores the source-branch version with `git_restore_file`
    and leaves the file out of the PR;
  - **Retry** — the agent revises the file against the reviewer's listed issues and
    re-reviews it; if it still scores low it reappears in the panel.
  Each click is sent to the agent as a chat turn, so the activity feed and run log
  show what it does; with several flagged files there is also **Approve all**. You can
  still type a free-form instruction instead.
- A reviewer failure (model error, unparseable reply) scores 0 and is flagged
  rather than silently passing.

Review scores go into the PR description. Configure with `REVIEWER_MODEL_ID`
(default `us.amazon.nova-pro-v1:0`), `REVIEWER_MODEL_REGION`, `REVIEW_MAX_DIFF_CHARS`,
or disable with `REVIEWER_ENABLED=false`.

## Make targets

```
make setup              Install dependencies, then start the Streamlit server
make install            Create the virtualenv and install requirements
make run                Launch the Streamlit application (foreground)
make start              Start it detached (logs to app.log)
make stop               Stop it (FORCE=1 to override the active-run guard)
make restart            Stop + start (FORCE=1 to override)
make status             pid, uptime, health, connected browsers, activity
make check              Verify toolchain, credentials, knowledge base and secrets
make secrets            Store the GitHub PAT in SSM
make infra              Terraform apply: knowledge base, guardrail, permissions
make infra-plan         Terraform plan only, change nothing
make infra-guardrail    Apply only the Bedrock guardrail
make infra-permissions  Apply only the IAM runtime policy (+ its dependencies)
make infra-sync         Re-upload knowledge-base/ documents and re-ingest
make infra-destroy      Terraform destroy everything (buckets included; PAT kept)
make clean              Remove Python caches
```

## Adding coding guidelines

Put documents in `knowledge-base/` (`.md`, `.txt`, `.pdf`, `.html`, `.docx`,
`.csv`, `.xlsx`) and run `./infra/deploy.sh --sync-only`. The agent is
instructed to check the knowledge base before making code changes and to apply
only the guidelines relevant to the change at hand.

A **guideline pack** is a `knowledge-base/<id>.pack.md` file whose YAML front matter drives
detection (`detect`), ordering (`depends_on`), file selection (`applies_to`) and the
completeness check (`acceptance`), followed by a `## transform` section written for the
agent. Detection picks up a new pack without a code change or restart; the knowledge base
needs the sync above. Schema and rule kinds: [docs/stages/02-discover.md](docs/stages/02-discover.md)
and [docs/file-selection.md](docs/file-selection.md).

## Configuration reference

All of these live in `.env` — see [.env.example](.env.example) for the full
annotated list.

| Group | Variables |
| --- | --- |
| AWS | `AWS_REGION`, `AWS_PROFILE` or `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` |
| Bedrock model | `BEDROCK_MODEL_ID`, `BEDROCK_MODEL_REGION`, `BEDROCK_MAX_TOKENS`, `BEDROCK_TEMPERATURE`, `BEDROCK_CONNECT_TIMEOUT`, `BEDROCK_READ_TIMEOUT`, `BEDROCK_MAX_ATTEMPTS`, `AGENT_RECURSION_LIMIT` (steps per turn, default 400), `AGENT_SUMMARY_ENABLED`, `AGENT_SUMMARY_TRIGGER_TOKENS`, `AGENT_SUMMARY_KEEP_MESSAGES`, `MAVEN_OUTPUT_MAX_CHARS` |
| Knowledge base (runtime) | `KNOWLEDGE_BASE_ID`, `KNOWLEDGE_BASE_REGION`, `KNOWLEDGE_BASE_NUM_RESULTS`, `KNOWLEDGE_BASE_DIRECTORY` |
| Parameter Store | `PARAMETER_STORE_PREFIX`, `SSM_PARAMETER_NAMES` |
| Guardrails | `GUARDRAIL_ID`, `GUARDRAIL_VERSION`, `GUARDRAIL_TRACE`, `SECRET_SCAN_ENABLED`, `SECRET_SCAN_ALLOWLIST` |
| Personal data scan | `PII_SCAN_ENABLED`, `PII_SCAN_GUARDRAIL_ID`, `PII_SCAN_GUARDRAIL_VERSION` (written by `deploy.sh`), `PII_SCAN_GLOBS`, `PII_SCAN_CHUNK_CHARS`, `PII_SCAN_MAX_CHARS` |
| Migration plan | `MIGRATION_PLAN_APPROVAL` |
| Pull request gate | `PR_GATE` (`block`, `draft`, `off`) |
| Reviewer | `REVIEWER_ENABLED`, `REVIEWER_MODEL_ID`, `REVIEWER_MODEL_REGION`, `REVIEWER_MAX_TOKENS`, `REVIEW_SCORE_THRESHOLD`, `REVIEW_MAX_DIFF_CHARS` |
| Git / GitHub | `GIT_COMMIT_AUTHOR_NAME`, `GIT_COMMIT_AUTHOR_EMAIL`, `GIT_SOURCE_BRANCH`, `GIT_BASE_BRANCH`, `GITHUB_API_TIMEOUT`, `GITHUB_API_VERSION` |
| Run defaults | `DEFAULT_GITHUB_URL`, `DEFAULT_UPGRADE_DETAILS` |
| Streamlit | `APP_ENTRYPOINT`, `APP_PAGE_TITLE`, `APP_PAGE_ICON`, `APP_CAPTION`, `STREAMLIT_SERVER_PORT`, `STREAMLIT_SERVER_ADDRESS`, `STREAMLIT_SERVER_HEADLESS` |
| Logging | `LOG_NAME`, `LOG_LEVEL`, `ROOT_LOG_LEVEL` |
| Local toolchain | `VENV_DIR`, `PYTHON_BIN`, `JAVA_HOME`, `JAVA_VERSION`, `WORK_ROOT` (sandbox root; short path on Windows) |
| Infrastructure | `PROJECT_NAME`, `CREATE_KNOWLEDGE_BASE`, `CREATE_GUARDRAIL`, `CREATE_PERMISSIONS`, `KB_NAME`, `KB_DESCRIPTION`, `KB_DATA_SOURCE_NAME`, `KB_EMBEDDING_MODEL_ID`, `KB_EMBEDDING_DIMENSIONS`, `KB_CHUNK_MAX_TOKENS`, `KB_CHUNK_OVERLAP_PERCENT`, `KB_VECTOR_BUCKET_NAME`, `KB_VECTOR_INDEX_NAME`, `KB_DISTANCE_METRIC`, `KB_DOCS_BUCKET_NAME`, `KB_DOCS_PREFIX`, `GUARDRAIL_NAME`, `GUARDRAIL_PII_ACTION`, `APP_IAM_PRINCIPAL`, `PRIMARY_BASE_MODEL_ID`, `REVIEWER_BASE_MODEL_ID` — each reaches Terraform as `TF_VAR_<lowercase>` |

Note: `GIT_SOURCE_BRANCH` is the branch the repo is cloned at and the upgrade
branch is cut from; `GIT_BASE_BRANCH` is the branch the pull request targets.
Both are single global settings, so one value applies to every repo.

## Troubleshooting

**`aws_s3vectors_index` fails on dimension** — `KB_EMBEDDING_DIMENSIONS` must be
between 1 and 4096 *and* a size the embedding model actually emits. Titan v2
supports 1024, 512 and 256.

**`Error acquiring the state lock`** — a previous run was interrupted. Confirm
nothing else is applying, then `terraform -chdir=infra/terraform force-unlock <id>`.

**Resource already exists on apply** — something with the same name exists
outside the state (for example left over from a manual run). Either import it
(`terraform -chdir=infra/terraform import <address> <id>`) or delete it and
re-apply; nothing in this deployment is retained on destroy, so this only happens
for resources created some other way.

**Knowledge base returns nothing** — check that an ingestion job succeeded:

```bash
aws bedrock-agent list-ingestion-jobs \
  --knowledge-base-id "$KNOWLEDGE_BASE_ID" --data-source-id <id>
```

**`ValidationException: managedSearchConfiguration is only supported for managed
knowledge bases`** — the retriever was built with the wrong search config for a
*vector* knowledge base. `load_kb_tool()` in [python/agent.py](python/agent.py) must
use `vectorSearchConfiguration` (it does now). A failed lookup no longer aborts the
turn: the tool returns an error string and the agent continues without guidelines,
so if you see this in the chat it is the tool's message, not a crash.

**Agent starts but has no guidelines tool** — expected when no knowledge base
id resolves; the startup log says so. Run `./infra/deploy.sh --sync`.

**Agent says "Unable to access cloned files"** — the file tools are sandboxed to
the per-run temp dir, so a clone that landed anywhere else is invisible to them.
Every path-taking tool now anchors relative paths to that sandbox and refuses
paths outside it, and the prompt tells the agent to clone into `repo/` and use
`repo/...` paths. If you still see it, check `app.log` for the `Cloning repo … to`
line: the target must be under the temp dir.

**Clone fails with a credential prompt or 401** — the PAT in SSM is missing,
expired or lacks access to the repo. `DEFAULT_GITHUB_URL` may be the https or
ssh form, but it must end in `.git` or name `owner/repo` on github.com;
anything else raises a clear `ValueError` at startup.

**Pull request fails with 422 `Field 'head' is invalid`** — nothing was pushed,
so there is no branch to open a PR from. Check the `create_branch` result in the
log. A 422 on `base` instead means `GIT_BASE_BRANCH` names a branch that does
not exist in the target repo.

**Git operations fail (401/403)** — the PAT in SSM needs `repo` scope (or
Contents + Pull requests read/write) and push rights on the target repo.

**401 "Bad credentials" although the token is correct** — an invisible character
came along with the paste (an ESC byte or zero-width space is typical for a hidden
terminal prompt). Symptom: the stored value is 41 characters where a classic PAT
is exactly 40, and `git` rejects the clone URL as "malformed". Re-run
`./infra/put_ssm_parameters.sh`; it now strips non-printable characters and
refuses anything that is not token-shaped. Check what is stored with
`aws ssm get-parameter --name forge_tool_api_key --with-decryption --query Parameter.Value --output text | wc -c`
(expect 41 including the trailing newline).

**Tests fail with an unresolved `…-SNAPSHOT` sibling dependency** — a multi-module
repo built in reactors. The agent has `run_maven_install` for exactly this: install
the parent/BOM and shared modules in build order, then test. It is told to look at
the repo's build scripts for that order.

**Every model call is blocked by the guardrail** — a secret pattern is in the
*input*. The PAT is kept out of the prompt by design, so check whether a tool
result (a file the agent read) contains a key; the regex filters are detect-only
on input for exactly this reason, but PII entities are not. Set
`GUARDRAIL_TRACE=true` to see which filter fired.

**`AccessDeniedException` on `ApplyGuardrail` or `Retrieve`** — the calling
identity lacks the runtime policy, or it was deployed before the guardrail / KB
existed and is still scoped to `*`-less placeholders. Re-run `./infra/deploy.sh`
(a full apply re-scopes it).

**Reviewer scores every file 0** — Nova Pro model access is not enabled, or the
principal cannot invoke `us.amazon.nova-pro-v1:0`. The reviewer never fails
silently: a 0 with `reviewer error:` in the chat panel means the call failed.

**A legitimate line is flagged as a secret** — add a regex to
`SECRET_SCAN_ALLOWLIST` for that line, or tell the agent to read the value from an
environment variable instead, which is usually the right fix anyway.

## Current state

The tool accepts a git repository and upgrade details, clones and modifies the
repo, consults the knowledge base, runs unit tests, has every changed file scored
by a second model with low scores routed to manual review, and submits a PR —
with secrets blocked at write, commit and model-output time.

## TODO

- Chat functionality ✔
- Knowledge base for company coding guidelines ✔
- Configuration externalized to `.env` ✔
- Infrastructure as code (Terraform) ✔
- Knowledge base on S3 Vectors (no idle capacity cost) ✔
- Guard rails: secret scanning + Bedrock Guardrail, PAT out of the prompt ✔
- Reviewer agent (Nova Pro) with manual review in chat ✔
- IAM runtime permissions as code ✔
- Migrated from CloudFormation to Terraform; destroy is clean, no retained orphans ✔
- Test harness
- Deterministic file inventory and per-pack acceptance checks ([docs](docs/file-selection.md)) ✔
- Reviewer sees the whole file, not only the diff (fixes speculative "incomplete migration" flags) ✔
- PR gate on tool results, generated verification section, test-parity check ✔
- Reviewer judges against the pack guidance; Retry may decline wrong issues ✔
- Prompt-attack filter on untrusted repo content (needs input tagging)
- Better, more complex test repo(s) (including Struts, etc.)
