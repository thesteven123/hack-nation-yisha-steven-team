# Scientesis

A modular, human-guided research lab for reproducible reinforcement-learning experiments. This simulation-only MVP includes an approval-gated Gymnasium Reacher-v5 PPO runner, reproducibility-focused critic, reviewed dataset intake, durable decision cards, and a separate scientist interpretation record. It is not a physical-robot safety system.

## Start locally

You need Python 3.11 or 3.12. The first install downloads the Python packages, including PyTorch and MuJoCo bindings; expect a sizable download and allow several minutes for setup.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m streamlit run app.py
```

If Streamlit reports `ModuleNotFoundError: No module named 'scientesis'`, activate the environment in the project root and run `python -m pip install -e '.[dev]'`. The editable install exposes the `src/` package. Virtual environments are tied to their original folder; if you moved the project, recreate `.venv` before installing.

On Windows PowerShell, activate with `file .venv\Scripts\Activate.ps1` instead. The first app launch creates `file data/scientesis.sqlite3` and seeds the example ResearchBrief. The core local workflow needs no API keys. No separate MuJoCo download, account, or license is required; its Python package is installed with the project.

In the app, save a proposal, approve that exact configuration, and then press **Start approved experiment**. Training uses the approved 100,000-step budget and evaluates 100 episodes in clean and noisy conditions; the run can take several minutes or longer on a CPU. Nothing trains automatically on launch.

Run the tests with:

```bash
python -m pytest
```

## Optional external retrieval

Evidence snapshots can be created locally without credentials. Firecrawl public-page capture works without a key for initial access; optionally set `FIRECRAWL_API_KEY` in the same terminal before launching Streamlit for higher limits.

Moss semantic retrieval is optional. Install its extra and set credentials in the shell session where Streamlit will run:

```bash
python -m pip install -e '.[retrieval]'
export MOSS_PROJECT_ID='your-project-id'
export MOSS_PROJECT_KEY='your-project-key'
python -m streamlit run app.py
```

`MOSS_INDEX_NAME` is optional (default `scientesis-research`). Moss sync and search send approved research context or search queries to Moss's cloud; raw uploaded datasets are excluded. The app sends nothing to Moss until you click **Sync** or **Search**. Keep keys out of source files, `.env` files tracked by Git, and commits. See the [Moss quickstart](https://docs.moss.dev/docs/start/quickstart) for obtaining project credentials.

## Project map

- `file app.py` — Streamlit interface only; business rules live in services and the repository.
- `file configs/research_brief.example.json` — bounded starter question, metrics, seed set, and permitted interventions.
- `src/scientesis/domain/` — typed experiment configuration.
- `src/scientesis/db/` — SQLite schema, persistence, approvals, and audit trail.
- `src/scientesis/services/` — target/hypothesis, authorization, dataset, decision, interpretation, evidence, and critique validation.
- `src/scientesis/adapters/` — isolated clients for optional Firecrawl and Moss services.
- `src/scientesis/rl/` — observation-noise and actuator-limit wrappers plus the experiment runner.
- `src/scientesis/ui/` — isolated research-target, dataset-intake, evidence-review/retrieval, decision-card, and scientist-interpretation screens.
- `tests/` — focused tests for authorization, persistence, critique grouping, wrappers, data review, evidence capture, snapshots, retrieval, decision scope, and human interpretation.
- `artifacts/` — checkpoints and run logs; generated files are excluded from version control.
- `file docs/scientesis_design_spec.md` — the supplied design document, kept with the project.

Each area is intentionally small and independently testable so later work can happen one module at a time rather than loading the whole design into a single coding pass.

## Current boundaries and next modules

The ResearchBrief editor creates immutable versions, and hypotheses stay drafts until a scientist reviews them; only approved hypotheses from the active brief version can be linked to proposals. Dataset intake, scoped decision records, and scientist interpretations are implemented as separate modules. Decision answers do not grant experiment permission; interpretations remain separate from critic verdicts. There is no LLM agent orchestration yet. The runner records versions, configuration, metrics, and model artifacts. The critic uses deterministic thresholds and requires a replicated matched baseline before it can report `supported`; that verdict remains a recommendation, not a scientist's interpretation. Safety is only an actuator-saturation simulation proxy, not physical-robot evidence.

Firecrawl-backed public-page capture, source approval, immutable evidence snapshots, and Moss indexing/search are implemented as optional modules. Firecrawl accepts an optional `FIRECRAWL_API_KEY`; Moss requires the optional `retrieval` extra and `MOSS_PROJECT_ID`/`MOSS_PROJECT_KEY`. Moss corpus sync is user-triggered and excludes uploaded datasets. Bright Data discovery is deferred unless curated-source capture proves insufficient; agent orchestration and ElevenLabs narration remain future slices.