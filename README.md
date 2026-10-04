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

## Optional LLM-assisted synthesis

Synthesis is optional and uses a single OpenAI-compatible Chat Completions endpoint with JSON mode. It adds no package dependency. Set these variables in the same shell session used to launch Streamlit:

```bash
export SCIENTESIS_LLM_API_KEY='your-provider-key'
export SCIENTESIS_LLM_MODEL='your-chat-model'
export SCIENTESIS_LLM_BASE_URL='https://api.openai.com/v1'
python -m streamlit run app.py
```

`SCIENTESIS_LLM_BASE_URL` is optional for the default endpoint; for self-hosted providers, use HTTPS or a local loopback HTTP endpoint. The **LLM synthesis** tab sends only the active ResearchBrief and the records captured in the snapshot you select, and only after you click **Generate**. Uploaded datasets and unselected records are excluded. The model has no tools. Outputs that fail strict brief, configuration, seed, threshold, or citation checks are rejected. Saving creates an unapproved proposal and draft hypothesis; you must review the hypothesis and separately approve the exact proposal before any run can start. No API key is needed for the rest of the local app.

On Windows PowerShell, set the same variables with `$env:SCIENTESIS_LLM_API_KEY = '...'` and `$env:SCIENTESIS_LLM_MODEL = '...'` in the launching terminal.

## Optional Lab Director audio

The **Runs & critique** page builds a read-only text summary from completed-run metrics, the stored critic verdict, the separate scientist interpretation, and exact current proposal approvals. Text summaries require no key. Audio is generated only when you review the text, consent to sending it, and click **Hear lab summary**.

For ElevenLabs playback, set credentials in the terminal used to launch the app:

```bash
export ELEVENLABS_API_KEY='your-elevenlabs-key'
export ELEVENLABS_VOICE_ID='a-voice-id-your-account-can-use'
python -m streamlit run app.py
```

`ELEVENLABS_MODEL_ID` is optional (default `eleven_multilingual_v2`). Obtain the key and a permitted voice ID from your ElevenLabs account. No SDK, additional download, or local audio configuration is required. Provider charges may apply. On PowerShell, use `$env:ELEVENLABS_API_KEY = '...'` and `$env:ELEVENLABS_VOICE_ID = '...'` in the launching terminal. On Zo, store these credentials in Settings → Advanced → Secrets instead of source files.

The app saves the MP3, exact text, hashes, and captured-state manifest under `artifacts/audio/`, and records the artifact in the local audit trail. These files are excluded from Git. Old audio is labeled historical when the current research state differs. Narration never approves a proposal, starts training, or converts a critic verdict into a scientist's interpretation. See the [ElevenLabs API documentation](https://elevenlabs.io/docs/api-reference/text-to-speech/convert).

## Project map

- `file app.py` — Streamlit interface only; business rules live in services and the repository.
- `file configs/research_brief.example.json` — bounded starter question, metrics, seed set, and permitted interventions.
- `src/scientesis/domain/` — typed experiment configuration.
- `src/scientesis/db/` — SQLite schema, persistence, approvals, and audit trail.
- `src/scientesis/services/` — target/hypothesis, authorization, deterministic planning, schema-validated synthesis, dataset, decision, interpretation, evidence, critique validation, and grounded narration.
- `src/scientesis/adapters/` — isolated clients for optional Firecrawl, Moss, OpenAI-compatible LLM, and ElevenLabs services.
- `src/scientesis/rl/` — observation-noise and actuator-limit wrappers plus the experiment runner.
- `src/scientesis/ui/` — isolated research-target, deterministic planner, snapshot-grounded synthesis, dataset-intake, evidence-review/retrieval, decision-card, scientist-interpretation, and Lab Director screens.
- `tests/` — focused tests for authorization, persistence, critique grouping, wrappers, data review, evidence capture, snapshots, retrieval, synthesis validation, decision scope, human interpretation, and mocked narration audio.
- `artifacts/` — checkpoints and run logs; generated files are excluded from version control.
- `file docs/scientesis_design_spec.md` — the supplied design document, kept with the project.

Each area is intentionally small and independently testable so later work can happen one module at a time rather than loading the whole design into a single coding pass.

## Current boundaries and next modules

The ResearchBrief editor creates immutable versions, and hypotheses stay drafts until a scientist reviews them; only approved hypotheses from the active brief version can be approved with proposals. Dataset intake, scoped decision records, and scientist interpretations are separate modules. Decision answers do not grant experiment permission; interpretations remain separate from critic verdicts. The deterministic Planner ranks bounded next work and can save only unapproved drafts; it does not call an LLM or start experiments. A separate, optional LLM synthesis tab can draft a hypothesis and proposal from one selected evidence snapshot, but strict deterministic validation and human review remain mandatory. The runner records versions, configuration, metrics, and model artifacts. The critic uses deterministic thresholds and requires a replicated matched baseline before it can report `supported`; that verdict remains a recommendation, not a scientist's interpretation. Safety is only an actuator-saturation simulation proxy, not physical-robot evidence.

Firecrawl-backed public-page capture, source approval, immutable evidence snapshots, and Moss indexing/search are implemented as optional modules. Firecrawl accepts an optional `FIRECRAWL_API_KEY`; Moss requires the optional `retrieval` extra and `MOSS_PROJECT_ID`/`MOSS_PROJECT_KEY`. Moss corpus sync is user-triggered and excludes uploaded datasets. Bright Data discovery is deferred unless curated-source capture proves insufficient. Lab Director text summaries and optional ElevenLabs playback are implemented; presentation polish remains the next slice.