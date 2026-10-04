# forge-tool docs

| Document | What it covers |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | components, turn model, tool contracts, security, infrastructure |
| [file-selection.md](file-selection.md) | how files are picked for migration: today, the target flow, files shared by several packs |
| [WINDOWS.md](WINDOWS.md) | running forge natively on a Windows PC (PowerShell), app only |
| [stages/01-prepare.md](stages/01-prepare.md) | clone, secret scan, test baseline |
| [stages/02-discover.md](stages/02-discover.md) | tech-stack detection, pack matching, knowledge base |
| [stages/03-plan.md](stages/03-plan.md) | the migration plan and the approval gate |
| [stages/04-migrate.md](stages/04-migrate.md) | edits, secret guard, tests vs baseline, branch and push |
| [stages/05-review-and-pr.md](stages/05-review-and-pr.md) | reviewer model, manual review, pull request |

## A run at a glance

```mermaid
flowchart LR
    s1["1 Prepare<br/>clone, scan,<br/>baseline"]
    s2["2 Discover<br/>stack, packs,<br/>guidance"]
    s3{"3 Plan<br/>user approves"}
    s4["4 Migrate<br/>edit, test,<br/>push"]
    s5{"5 Review<br/>user decides<br/>flagged files"}
    pr["Pull request"]
    s1 --> s2 --> s3 --> s4 --> s5 --> pr
    classDef gate fill:#FDE8D8,stroke:#D9742B,color:#14213D
    class s3,s5 gate
```

The two orange stages are hard stops: the agent waits for the user before it continues.
