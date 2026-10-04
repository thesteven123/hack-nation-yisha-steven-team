# AI Lab quick start

This branch is an independent development copy. Its launcher uses a separate app profile and loopback server. It does not install an update over your existing AgentsDock app. The competition copy opens in English. New agent summaries, ideas, reviews and questions are requested in English; your existing research and exact quotations retain their original language.

## Start here

On a prepared Windows computer, run `Start-AILab.cmd` from the repository root. Choose **Idea Lab** or **Research workspace** in the sidebar. The server indicator must be **Online**.

- **Idea Lab** helps you find relevant literature and compare research directions. Enter a research goal; pasted sources are optional. **Start literature research** begins bounded web discovery and model work.
- **Research workspace** takes a question or a saved Idea choice into a versioned plan. Choose an available method, supply its required inputs, and save the plan. You must separately select an action and click **Run the selected frozen action** before execution.
- **Trash** contains research removed from the active list. Click the trash icon next to a research item, inspect its title, and choose **Move to Trash**. **Cancel** changes nothing. Choose **Restore** in Trash to return it to the active list. Stop any running generation or native role first. Deletion retains saved evidence, results, history and links; it is not permanent file erasure or automatic cancellation.

## Read results without reading every log

The Idea group has two members: **Literature Agent** retrieves sources and extracts evidence; **Idea Agent** compares directions and reviews them in an independent task. Read their concise status, the papers inspected, findings, evidence gaps and proposed next steps first. Expand evidence to inspect exact quotations and coverage. Technical logs and native task receipts are collapsed by default.

Missing accessible evidence is not evidence that a paper or effect does not exist. An abstract, partial text or retained full original is explicitly labeled. Saving a complete original does not mean a model has read all of it. Recommendations remain proposals; choosing a direction does not validate it or authorize experiments.

Continue literature research when further reading is useful and the bounded budget permits it. Answer required questions first. Starting another run is an explicit action; the app does not automatically rerun saved research just because you open it.

## Research workflow

1. Start with a question or hypothesis, or import a saved current Idea selection and its exact frozen evidence.
2. Define a useful outcome, constraints and budget. Optionally record competing hypotheses with their assumptions, predictions and weakening conditions.
3. Inspect candidates, explicitly select one, then separately run the frozen action.
4. Inspect observations, quality checks, interpretations, unresolved questions and next-step recommendations. Planner, Analyst and Reviewer tasks are optional, independently requested model interpretations. They do not change machine results or execute actions for you.
5. Continue by explicitly freezing another plan, revising the goal, correcting inputs, deferring, or stopping. Old results and criteria remain available.

Available local adapters inspect supplied source text or analyze supplied paired numbers. They do not execute arbitrary scientific experiments. Same-data sensitivity is not independent replication. If the available method does not answer your question, record the missing method instead of treating a demo as a scientific result.

Branches are optional. Explicitly enable up to three questions within a campaign when separate paths are useful. They share the campaign budget; one waiting for your answer does not by itself block an unrelated branch. Declared dependencies still gate work. Shared evidence corrections require explicit reconciliation and revalidation, preserving historical observations.

## Prepare another computer

Windows requires PowerShell 7+, Node.js 22+, pnpm 11.9.0, WSL, and the Linux server environment. Prepare dependencies explicitly:

```powershell
Set-Location electron
pnpm install --frozen-lockfile
pnpm typecheck
pnpm build
Set-Location ..
./Start-AILab.ps1
```

The launcher does not install dependencies, build code or generate an installer. When dependencies already exist, `npm run typecheck` and `npm run build` also work. See the [Electron README](../electron/README.md).

The Linux server needs Python >=3.10 and dependencies in [server/pyproject.toml](../server/pyproject.toml). The selected WSL user needs a working `codex` CLI and native login. Preparation copies that login into an isolated CLI home; research consumes the account's model quota. Existing chats, projects and MCP configuration are not copied.

Launcher defaults are `Ubuntu` / `agentsdock`, with Python at `/home/agentsdock/.cache/agentsdock-ai-lab-dev/server-venv/bin/python` and development state at `/home/agentsdock/.cache/agentsdock-idea-lab-dev`. The Windows profile is `%LOCALAPPDATA%\Programs\AgentsDockIdeaLabData`. Another machine must prepare those paths or supply `-WslDistribution`, `-WslUser`, `-ServerPython` and `-ServerRoot`.

Use `./Start-AILab.ps1 -ServerOnly` to start only the dedicated service. To load changed server code, verify no research or model job is running, close the development window, use `./Start-AILab.ps1 -StopServer`, then launch again. The stop command checks the marked development service. Startup errors are recorded in the development profile's `logs\server-start\server.stderr.log`; an unrelated port owner is not automatically stopped.

## Current limits

Full v0.5 remains partially accepted. Selected-model exact tokenizer admission and Research model on-demand reading of complete retained evidence (E4 resolver and backup proof) remain unintegrated. Existing summaries or bounded packets do not prove the model inspected every saved source. General scientific execution, cross-domain validity, independent scientific replication and a 10x speed improvement are not established.

See [acceptance and limitations](AI_LAB_ACCEPTANCE.md), [Idea Lab behavior](../server/docs/IDEA_LAB.md), [branch design](../server/docs/RESEARCH_BRANCHES_DESIGN.md), and [actual app acceptance requirements](APP_DEV_OPERATIONS.md) for verified scope.
