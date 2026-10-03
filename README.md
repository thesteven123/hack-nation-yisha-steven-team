# Scientesis

A modular, human-guided research lab for reproducible reinforcement-learning experiments. This simulation-only MVP includes an approval-gated Gymnasium Reacher-v5 PPO runner, reproducibility-focused critic, reviewed dataset intake, durable decision cards, and a separate scientist interpretation record. It is not a physical-robot safety system.

## Start locally

You need Python 3.11 or 3.12. The first install downloads the Python packages, including PyTorch and MuJoCo bindings; expect a sizable download and allow several minutes for setup.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
streamlit run app.py
```

On Windows PowerShell, activate with `file .venv\Scripts\Activate.ps1` instead. The first app launch creates `file data/scientesis.sqlite3` and seeds the example ResearchBrief. No credentials or API keys are required for the implemented workflow. No separate MuJoCo download, account, or license is required; its Python package is installed with the project.

In the app, save a proposal, approve that exact configuration, and then press **Start approved experiment**. Training uses the approved 100,000-step budget and evaluates 100 episodes in clean and noisy conditions; the run can take several minutes or longer on a CPU. Nothing trains automatically on launch.

Run the tests with:

```bash
python -m pytest
```

## Project map

- `file app.py` — Streamlit interface only; business rules live in services and the repository.
- `file configs/research_brief.example.json` — bounded starter question, metrics, seed set, and permitted interventions.
- `src/scientesis/domain/` — typed experiment configuration.
- `src/scientesis/db/` — SQLite schema, persistence, approvals, and audit trail.
- `src/scientesis/services/` — target/hypothesis, authorization, dataset, decision, interpretation, and critique validation.
- `src/scientesis/rl/` — observation-noise and actuator-limit wrappers plus the experiment runner.
- `src/scientesis/ui/` — isolated research-target, dataset-intake, decision-card, and scientist-interpretation screens.
- `tests/` — focused tests for authorization, persistence, critique grouping, wrappers, data review, decision scope, and human interpretation.
- `artifacts/` — checkpoints and run logs; generated files are excluded from version control.
- `file docs/scientesis_design_spec.md` — the supplied design document, kept with the project.

Each area is intentionally small and independently testable so later work can happen one module at a time rather than loading the whole design into a single coding pass.

## Current boundaries and next modules

The ResearchBrief editor creates immutable versions, and hypotheses stay drafts until a scientist reviews them; only approved hypotheses from the active brief version can be linked to proposals. Dataset intake, scoped decision records, and scientist interpretations are implemented as separate modules. Decision answers do not grant experiment permission; interpretations remain separate from critic verdicts. There is no LLM agent orchestration yet. The runner records versions, configuration, metrics, and model artifacts. The critic uses deterministic thresholds and requires a replicated matched baseline before it can report `supported`; that verdict remains a recommendation, not a scientist's interpretation. Safety is only an actuator-saturation simulation proxy, not physical-robot evidence.

The design document's Firecrawl, Bright Data, Moss, and ElevenLabs integrations are not wired in. Each would need a separate adapter and provider credentials if enabled; none is required for the current MVP. No API keys are stored in this project.# hack-nation-yisha-steven-team
