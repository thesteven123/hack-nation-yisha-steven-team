# Scientesis: Human-Guided Agentic Research Lab

**Document type:** implementation-ready design specification  
**Status:** hackathon MVP architecture  
**Primary domain:** reinforcement learning (RL) and simulated robotics  
**Principle:** scientists retain research authority; agents accelerate retrieval, protocol design, execution, critique, and documentation.

---

## 1. Product Summary

Scientesis is a human-guided, agentic scientific-discovery workspace. It turns a research objective into a governed, reproducible cycle:

```text
Research question
  → evidence retrieval
  → hypothesis and protocol proposal
  → human review / edit / approval
  → experiment execution
  → structured results
  → critique and human interpretation
  → evidence-backed next decision
```

The MVP studies a narrow computer-science question using RL in a simulated robotics/control environment:

> Under a fixed training budget, which intervention improves a simulated robot policy’s performance under noisy sensing while maintaining acceptable safety behavior?

Suggested initial environment: Gymnasium `Reacher` or `Pendulum`. Suggested algorithm: PPO using Stable-Baselines3.

Scientesis is **not** an autonomous scientist that silently decides research goals or runs arbitrary experiments. It is a collaborative laboratory system that:

- Converts scientist intent into a falsifiable protocol.
- Retrieves and preserves evidence provenance.
- Presents multiple options with reasons, risks, and alternatives.
- Requires human authorization for experiments outside existing approval coverage.
- Keeps durable, versioned records of human and AI decisions.
- Separates external literature, local experimental data, AI inference, and human judgment.

---

## 2. Goals and Non-Goals

### 2.1 Goals

1. Run a credible closed-loop scientific workflow using real simulator-generated results.
2. Let scientists submit research questions, hypotheses, data, constraints, annotations, and custom directions.
3. Require explicit, structured human approval before executing newly proposed experiments.
4. Preserve reproducibility: configurations, environment versions, seeds, metrics, artifacts, and decision provenance.
5. Retrieve relevant prior evidence and decisions before generating the next proposal.
6. Make the difference between **external prior evidence** and **local experimental findings** visible.
7. Demonstrate a controlled RL/robotics experiment campaign with at least one baseline, two variants, and a replication.
8. Provide a dashboard that makes the entire reasoning and approval loop legible in a short demo.

### 2.2 Non-Goals

- Do not claim to discover universal RL rules or solve general robotics.
- Do not train a custom foundation model.
- Do not implement an RL algorithm from scratch for the MVP.
- Do not crawl the entire web or ingest uncontrolled source corpora.
- Do not permit agents to execute arbitrary shell commands, downloaded code, or unbounded experiments.
- Do not infer authorization from silence, a missing answer, or an AI-inferred preference.
- Do not use web-retrieved claims as proof that an intervention worked locally.
- Do not rely on a vector/retrieval layer as the authoritative numeric experiment database.

---

## 3. Core Research Scenario

### 3.1 Initial research question

```text
Does training-time observation noise improve a simulated reaching policy's
performance under noisy sensing without causing unacceptable safety regressions?
```

### 3.2 Intervention space

The planner may only choose values defined by the active `ResearchBrief`.

| Variable | Initial allowed values | Purpose |
|---|---:|---|
| Training observation noise | 0.00, 0.02, 0.05 | Test robustness via noise augmentation |
| Evaluation observation noise | 0.00, 0.05 | Measure clean and noisy robustness |
| Safety penalty | 0.00, 0.25, 0.50, 1.00 | Map performance-safety tradeoff |
| Reward mode | default; dense-distance | Optional reward-shaping intervention |
| Training seed | 7, 19, 42 | Support replication |
| Training steps | 100,000 fixed | Keep comparisons controlled |

### 3.3 Metrics

| Metric | Meaning | Role |
|---|---|---|
| `clean_success_rate` | Success under clean evaluation | Secondary outcome |
| `noisy_success_rate` | Success with noisy observations | Primary outcome |
| `mean_episode_return` | Optimization signal | Diagnostic only; not sufficient alone |
| `mean_steps_to_goal` | Behavioral efficiency | Secondary outcome |
| `safety_violations_per_episode` | Hazard-zone incursions / safety cost | Safety outcome |
| `training_steps` | Environment interaction budget | Control / cost metric |
| `n_seeds` | Number of independent training runs | Evidence-quality metric |

### 3.4 Scientific validity rules

1. A claim of improvement requires results from at least three seeds unless the scientist explicitly marks it as exploratory.
2. Comparisons must use the same environment version, algorithm, policy architecture, training budget, and evaluation protocol.
3. Reward alone cannot establish task success; success rate must be reported.
4. Any finding based on one seed must be labelled `provisional`.
5. The critic must flag differing evaluation episode counts, untracked code/config changes, or missing safety metrics.
6. Simulation results must be described as simulation results; no claim of physical-world deployment readiness.

---

## 4. Architecture Overview

```text
                         ┌────────────────────────────────────┐
                         │           Scientist UI             │
                         │ questions, hypotheses, data, edits │
                         │ approvals, interpretations         │
                         └───────────────┬────────────────────┘
                                         │
                                         v
┌─────────────────────────────────────────────────────────────────────────┐
│                         Decision Service                                │
│ ResearchBrief versions | DecisionRecords | scope | authorization state   │
└───────────────┬──────────────────────────────┬──────────────────────────┘
                │                              │
                v                              v
┌──────────────────────────┐        ┌──────────────────────────┐
│ Research Librarian       │        │ Idea Agent / Planner     │
│ Firecrawl / Bright Data  │        │ hypotheses, options,     │
│ evidence cards           │        │ experiment proposals     │
└───────────────┬──────────┘        └──────────────┬───────────┘
                │                                  │
                └───────────────┬──────────────────┘
                                v
                      ┌─────────────────────┐
                      │ Moss Retrieval Layer│
                      │ searchable memory   │
                      └──────────┬──────────┘
                                 │
                                 v
              ┌───────────────────────────────────────┐
              │ SQLite / DuckDB (source of truth)      │
              │ data, configs, approvals, results      │
              └──────────────┬────────────────────────┘
                             │ only approved configs
                             v
                  ┌────────────────────────────┐
                  │ Experiment Runner           │
                  │ Gymnasium + SB3 PPO         │
                  └──────────────┬─────────────┘
                                 v
                  ┌────────────────────────────┐
                  │ Critic + Coordinator        │
                  │ audit, confidence, next step│
                  └──────────────┬─────────────┘
                                 v
                  ┌────────────────────────────┐
                  │ Lab Director                │
                  │ dashboard + ElevenLabs TTS  │
                  └────────────────────────────┘
```

### 4.1 Source-of-truth rules

| System | Responsibility | Not responsible for |
|---|---|---|
| SQLite / DuckDB | Exact metrics, configs, seeds, approvals, artifacts, statuses, versioned records | Semantic retrieval and free-form explanations |
| Moss | Searchable lab memory: evidence cards, summaries, critique notes, decisions, constraints | Final numeric calculation or authorization decisions |
| Firecrawl | Retrieve selected web pages as clean Markdown / JSON | Establishing local empirical truth |
| Bright Data | Discover public sources; fallback retrieval for public pages where needed | Circumventing logins, paywalls, site access rules, or permissions |
| Stable-Baselines3 + Gymnasium | Execute approved RL experiments | Choosing research priorities |
| LLM agents | Propose, summarize, critique, explain | Silent authorization, raw metric fabrication, uncontrolled execution |
| ElevenLabs | Accessible, evidence-grounded audio narration | Generating unvalidated research facts |

---

## 5. Agent Design

### 5.1 Research Librarian

**Responsibilities**

- Retrieve a small curated source packet for a defined research uncertainty.
- Prefer official docs, benchmark maintainers, peer-reviewed work, technical reports, and reputable preprints.
- Extract a claim, scope, limitation, and implementation relevance.
- Create provenance-rich `EvidenceCard` records.
- Index approved summaries into Moss.

**Inputs**

- Active `ResearchBrief`.
- Specific evidence query, e.g. `RL observation noise robustness continuous control`.
- Allowed public-source policies.

**Outputs**

- Candidate source list.
- Approved evidence cards.
- Relevant Moss document IDs.

**Never**

- Treat literature as proof of local results.
- Download or execute code from a retrieved source.
- Crawl broad domains without a scope limit.

### 5.2 Idea Agent / Planner

**Responsibilities**

- Review active brief, completed experiments, critic reports, external evidence, and explicit human constraints.
- Produce a falsifiable hypothesis and bounded experiment proposal.
- When multiple plausible directions exist, generate a `DecisionRecord` with 2–4 alternatives.
- Include a `Modify target` path and support custom free-text direction.
- Identify which work is already authorized and which work is blocked by a pending decision.

**Inputs**

- Active `ResearchBrief` version.
- Evidence snapshot.
- Existing decision/feedback records.
- Structured experiment history from SQLite/DuckDB.

**Outputs**

- `Hypothesis` draft.
- `ExperimentProposal` draft.
- Optional `DecisionRecord` in `pending` state.

**Never**

- Propose values outside the active brief without asking to modify the target.
- Treat an AI-inferred preference as a hard constraint.
- Queue execution directly.

### 5.3 Decision Service

**Responsibilities**

- Persist and version research briefs, decision questions, options, answers, scopes, and evidence snapshots.
- Enforce approval and authorization rules.
- Distinguish human-explicit preferences from AI-inferred preferences.
- Ensure no-response remains `pending`, never an implicit selection.
- Update the `ResearchBrief` only after an explicit qualifying decision.

**Implementation note:** This is a deterministic service, not an LLM agent.

### 5.4 Experiment Runner

**Responsibilities**

- Validate that a configuration is complete, permitted, and approved.
- Create the environment and wrappers.
- Train PPO using Stable-Baselines3.
- Evaluate under predefined clean/noisy/safety protocols.
- Log raw metrics, config, seed, versions, artifacts, and errors.
- Generate a factual run summary for Moss.

**Never**

- Execute unapproved experiments.
- Accept arbitrary code or shell commands generated by an LLM.
- Modify metrics to make a hypothesis appear true.

### 5.5 Critic

**Responsibilities**

- Check comparability and protocol adherence.
- Check replication count and evaluation consistency.
- Identify confounds, missing metrics, and overclaims.
- Produce a verdict: `supported`, `provisional`, `inconclusive`, or `invalid_comparison`.
- Recommend replication or bounded next tests.

### 5.6 Coordinator

**Responsibilities**

- Rank authorized next work using explicit heuristics.
- Continue independent work when decisions are pending.
- Hold branches that depend on an unanswered decision.
- Create decision needs when no authorized action can resolve a high-priority uncertainty.

### 5.7 Lab Director

**Responsibilities**

- Translate validated state into concise text and optional spoken summaries.
- Explain the current conclusion, limitation, and next decision.
- Use only verified metrics and stored verdicts.

---

## 6. Human-in-the-Loop Model

### 6.1 Human authority

Scientists retain authority over:

- Research questions and project goals.
- Hypotheses and protocols.
- Parameter-range expansion.
- Dataset acceptance and handling of quality warnings.
- Experiment authorization where not already covered.
- Interpretation status of findings.
- Publication/export of conclusions.

### 6.2 Research workspace intake

Create a `New Research Project` form with these fields:

| Field | Example |
|---|---|
| Project title | Robust Robot Learning Under Noisy Sensing |
| Research question | Does training-time observation noise improve noisy-condition success? |
| Objective | Improve robustness while limiting hazard violations |
| Environment | `Reacher-v5` |
| Algorithm | PPO |
| Primary outcome | Noisy success rate over 100 evaluation episodes |
| Secondary outcomes | Clean success, safety violations, return, steps to goal |
| Experiment budget | 12 runs |
| Training budget | 100,000 steps/run |
| Replication rule | Minimum 3 seeds for claims |
| Parameter limits | Noise ≤ 0.05; safety penalty ≤ 1.0 |
| Notes/caveats | Human-authored contextual constraints |

The submitted form creates `ResearchBrief v1`.

### 6.3 Hypothesis editor

A scientist may submit free text. The system transforms it into an editable structured hypothesis.

**Human text**

```text
I think moderate observation noise during training may make the policy more
robust to sensor error, but excessive noise could slow learning.
```

**Operationalized draft**

```text
If PPO is trained in Reacher-v5 with Gaussian observation noise σ = 0.05,
then mean noisy-evaluation success will exceed the no-noise baseline by at
least 10 percentage points, while clean success declines by no more than
5 percentage points.

Controls: PPO config, environment version, policy architecture, training steps,
evaluation episodes, and seed protocol.
```

Available actions:

- `Accept draft`
- `Edit protocol`
- `Reject`
- `Ask why this operationalization was chosen`
- `Save as exploratory`

### 6.4 Data contribution workflow

Support CSV, JSON, and Markdown in MVP.

On upload, create a `Dataset` record and show:

- Number of rows, columns, types.
- Missing values.
- Duplicate rows/keys.
- Range checks and schema validation.
- Collection method and provenance fields.
- File hash.
- Explicit options for how to handle warnings.

**Rule:** Never silently clean, impute, delete, or reinterpret scientist-provided data.

### 6.5 Approval lifecycle

```text
Draft
  → Proposed
  → Reviewed
  → Approved | Rejected | Revision requested
  → Queued
  → Running
  → Completed
  → Critiqued
  → Scientist interpretation
  → Accepted as evidence | Inconclusive | Invalid
```

Only an `Approved` proposal may be queued. The runner validates that its parameters remain within the authorization scope immediately before execution.

### 6.6 Pending decision behavior

If a scientist does not answer a question:

- Continue independent work covered by existing goals and authorization.
- Pause only branches that require that decision.
- Keep the decision state `pending`.
- Do not log a non-response as agreement, rejection, selection, or preference.

Example:

| Work item | Decision required? | Behavior |
|---|---:|---|
| Retrieve and summarize robust-RL sources | No | Continue |
| Index approved evidence in Moss | No | Continue |
| Summarize completed EXP-008 | No | Continue |
| Run an already-approved replication batch | No, if scope covers it | Continue |
| Choose reward shaping vs. safety penalty | Yes | Wait for decision |
| Increase max noise beyond 0.05 | Yes | Wait for target modification |
| Finalize a claim as supported | Yes | Wait for scientist interpretation |

---

## 7. DecisionRecord Specification

### 7.1 Purpose

`DecisionRecord` is a first-class, durable object. It records a decision point, options, supporting evidence snapshot, actual human response, decision scope, and corresponding changes to the research brief.

This prevents decisions from disappearing into chat logs and prevents temporary selections from silently becoming permanent preferences.

### 7.2 Required fields

| Field | Description |
|---|---|
| `decision_id` | Unique ID |
| `project_id` | Project relationship |
| `question` | The decision that could change the next step |
| `options` | Alternatives shown to the scientist |
| `recommended_option_id` | Recommendation, if any, clearly marked |
| `rationale` | Reason for each option and recommendation |
| `evidence_snapshot_id` | Exact evidence version used to create options |
| `human_answer` | Actual selected option, custom response, or defer |
| `human_rationale` | Optional human-provided reason |
| `scope` | Decision applicability and expiry |
| `pre_brief_version` | ResearchBrief version before response |
| `post_brief_version` | ResearchBrief version after response, if changed |
| `status` | Pending, answered, expired, superseded |

### 7.3 JSON example

```json
{
  "decision_id": "DR-017",
  "project_id": "robust-robot-learning",
  "decision_type": "next_experiment_direction",
  "question": "Which uncertainty should the lab prioritize next?",
  "options": [
    {
      "option_id": "A",
      "label": "Replicate noise-trained PPO across two additional seeds",
      "rationale": "The observed noisy-success improvement is based on one seed and cannot yet support a general claim.",
      "benefits": ["Reduces uncertainty", "Tests reproducibility"],
      "risks": ["Uses two experiment slots", "May show initial result was random"],
      "cost": {"experiments": 2, "training_steps": 200000},
      "evidence_refs": ["EXP-003", "EXP-008", "CRITIC-004"]
    },
    {
      "option_id": "B",
      "label": "Test a moderate safety penalty with noise training",
      "rationale": "Noise training improved robustness but increased hazard entries in EXP-008.",
      "benefits": ["Explores safety-performance tradeoff"],
      "risks": ["Adds a second factor before replicating robustness"],
      "cost": {"experiments": 1, "training_steps": 100000},
      "evidence_refs": ["EXP-008"]
    },
    {
      "option_id": "C",
      "label": "Modify research target",
      "rationale": "Change the primary metric, experiment budget, task, or allowed intervention values.",
      "benefits": ["Scientist keeps control of research direction"],
      "risks": [],
      "cost": {"experiments": 0, "training_steps": 0},
      "evidence_refs": []
    }
  ],
  "recommended_option_id": "A",
  "recommendation_rationale": "Replication provides the highest expected uncertainty reduction before adding another intervention.",
  "evidence_snapshot_id": "ES-2026-10-03-01",
  "human_answer": {
    "answer_type": "option_selected",
    "selected_option_id": "A",
    "human_rationale": "We need evidence across seeds before changing more than one variable."
  },
  "scope": {
    "applies_to": "current active ResearchBrief only",
    "valid_until": "replication batch completion",
    "creates_permanent_preference": false
  },
  "pre_brief_version": 4,
  "post_brief_version": 4,
  "status": "answered"
}
```

### 7.4 Decision-card UI requirements

Every decision card must display:

1. A concrete question.
2. Two to four actual alternatives.
3. A recommended option, if there is one, explicitly labeled.
4. Rationale, benefits, risks, estimated cost, and evidence for **every** option.
5. Source/experiment IDs used to make the recommendation.
6. `Modify target` option.
7. Free-text response input.
8. `Not deciding yet` control that leaves the record pending.

Do not hide alternatives behind an expandable control by default. Do not use wording that suggests the recommended option is mandatory.

---

## 8. Preference and Authorization Model

### 8.1 Preference categories

| Category | Meaning | Enforcement |
|---|---|---|
| `explicit_human` | Directly stated by a scientist | May constrain proposals within declared scope |
| `human_choice_current_stage` | A selection made for a specific decision | Applies only to declared decision/batch scope |
| `ai_inferred` | Pattern inferred by the system | Advisory only; cannot block alternatives or authorize work |
| `system_policy` | Platform safety/reproducibility rule | Enforced globally |

### 8.2 Rules

- A one-time selection does not become a global project preference automatically.
- AI-inferred preferences must be visibly labeled as inferred and include confidence.
- Inferred preferences cannot reduce the option set without human confirmation.
- `No response` is not a preference.
- Authorization must state explicit scope: experiment IDs, parameter limits, time window, run count, or budget.

### 8.3 Example preference records

```json
{
  "preference_id": "PREF-012",
  "statement": "Prioritize a quick validation experiment.",
  "source": "human_choice_current_stage",
  "scope": "decision DR-017 only",
  "expires_after": "DR-017 resolution"
}
```

```json
{
  "preference_id": "PREF-INFERRED-003",
  "statement": "The scientist may prefer low-compute experiments.",
  "source": "ai_inferred",
  "confidence": 0.42,
  "scope": "advisory_only",
  "status": "unconfirmed"
}
```

---

## 9. Data Model

### 9.1 Core entities

- `Project`
- `ResearchBriefVersion`
- `Hypothesis`
- `Dataset`
- `EvidenceCard`
- `EvidenceSnapshot`
- `DecisionRecord`
- `DecisionOption`
- `DecisionAnswer`
- `PreferenceRecord`
- `ExperimentProposal`
- `ExperimentRun`
- `MetricRecord`
- `CriticReport`
- `HumanInterpretation`
- `Artifact`
- `AuditEvent`

### 9.2 SQL schema (MVP)

```sql
CREATE TABLE research_projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE research_brief_versions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    brief_json TEXT NOT NULL,
    created_by TEXT NOT NULL,             -- human | agent | system
    change_reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, version)
);

CREATE TABLE hypotheses (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    original_text TEXT NOT NULL,
    operationalized_protocol_json TEXT,
    source TEXT NOT NULL,                 -- human | agent | coauthored
    status TEXT NOT NULL,                 -- draft | approved | rejected
    created_by TEXT NOT NULL,
    reviewed_by TEXT,
    review_note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE datasets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    storage_uri TEXT NOT NULL,
    source_type TEXT NOT NULL,            -- human_upload | web_retrieved | simulation
    provenance_json TEXT NOT NULL,
    validation_report_json TEXT,
    sha256 TEXT,
    uploaded_by TEXT NOT NULL,
    uploaded_at TEXT NOT NULL
);

CREATE TABLE evidence_cards (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    source_url TEXT,
    source_type TEXT NOT NULL,            -- official_docs | paper | preprint | technical_blog
    claim_text TEXT NOT NULL,
    scope_text TEXT,
    limitations_text TEXT,
    implementation_hint TEXT,
    retrieved_at TEXT NOT NULL,
    approved_by TEXT,
    moss_document_id TEXT
);

CREATE TABLE evidence_snapshots (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    moss_query TEXT,
    source_ids_json TEXT NOT NULL,
    experiment_ids_json TEXT NOT NULL,
    critic_report_ids_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE decision_records (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    question TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    status TEXT NOT NULL,                 -- pending | answered | expired | superseded
    evidence_snapshot_id TEXT NOT NULL,
    pre_brief_version INTEGER NOT NULL,
    post_brief_version INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    answered_by TEXT,
    answered_at TEXT
);

CREATE TABLE decision_options (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    label TEXT NOT NULL,
    rationale TEXT NOT NULL,
    benefits_json TEXT,
    risks_json TEXT,
    cost_estimate_json TEXT,
    evidence_refs_json TEXT NOT NULL,
    is_recommended BOOLEAN NOT NULL DEFAULT FALSE,
    is_modify_target_option BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE decision_answers (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    answer_type TEXT NOT NULL,            -- option_selected | custom_text | defer
    selected_option_id TEXT,
    custom_response TEXT,
    rationale_optional TEXT,
    scope_json TEXT NOT NULL,
    preference_source TEXT NOT NULL,      -- explicit_human | ai_inferred
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE preference_records (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    statement TEXT NOT NULL,
    source TEXT NOT NULL,                 -- explicit_human | human_choice_current_stage | ai_inferred | system_policy
    confidence REAL,
    scope_json TEXT NOT NULL,
    status TEXT NOT NULL,                 -- active | expired | unconfirmed | revoked
    created_at TEXT NOT NULL
);

CREATE TABLE experiment_proposals (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    hypothesis_id TEXT,
    research_brief_version INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL,                 -- draft | proposed | reviewed | approved | rejected | queued
    proposed_by TEXT NOT NULL,            -- human | planner_agent
    approval_scope_json TEXT,
    approved_by TEXT,
    approval_note TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT
);

CREATE TABLE experiment_runs (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    status TEXT NOT NULL,                 -- queued | running | completed | failed | cancelled
    environment_version TEXT,
    algorithm_version TEXT,
    seed INTEGER,
    config_json TEXT NOT NULL,
    metrics_json TEXT,
    artifact_manifest_json TEXT,
    started_at TEXT,
    completed_at TEXT,
    error_message TEXT
);

CREATE TABLE critic_reports (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    experiment_run_ids_json TEXT NOT NULL,
    verdict TEXT NOT NULL,                -- supported | provisional | inconclusive | invalid_comparison
    findings_json TEXT NOT NULL,
    limitations_json TEXT,
    recommended_next_action_json TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE human_interpretations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    critic_report_id TEXT NOT NULL,
    verdict TEXT NOT NULL,                -- accepted_evidence | preliminary | inconclusive | rejected
    rationale TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE audit_events (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_type TEXT NOT NULL,             -- human | agent | system
    payload_json TEXT,
    created_at TEXT NOT NULL
);
```

---

## 10. Retrieval and Evidence Design

### 10.1 Moss indexing policy

Index concise, provenance-rich documents:

- Active research brief summaries.
- Human-authored hypotheses and approved operationalizations.
- Completed experiment summaries.
- Critic reports.
- Human decisions, constraints, edits, rejections, and interpretations.
- Approved external evidence cards.
- Decision memos.
- Failure reports.

Do not index raw metric tables as the sole evidence source. The actual numeric truth remains in SQLite/DuckDB.

### 10.2 Moss metadata

```json
{
  "document_type": "experiment_summary",
  "project_id": "robust-robot-learning",
  "experiment_id": "EXP-008",
  "environment": "Reacher-v5",
  "algorithm": "PPO",
  "status": "completed",
  "topic": ["robustness", "observation_noise", "safety"],
  "noise_train": 0.05,
  "safety_penalty": 0.0,
  "seed": 42,
  "research_brief_version": 4,
  "created_at": "2026-10-03T11:12:00-07:00"
}
```

### 10.3 EvidenceCard structure

```json
{
  "evidence_id": "EV-004",
  "title": "Robust RL method documentation",
  "source_url": "https://example.org",
  "source_type": "official_docs",
  "claim": "Observation perturbation during training may improve robustness under sensor noise.",
  "scope": "Applies to simulated continuous-control tasks studied by the source.",
  "limitations": "Does not establish improvement for our Reacher environment or configuration.",
  "lab_use": "Motivates a controlled baseline versus noise-trained PPO comparison.",
  "retrieved_at": "2026-10-03T11:12:00-07:00"
}
```

### 10.4 Evidence snapshot rule

Whenever the Idea Agent creates a decision or proposal, save an `EvidenceSnapshot` containing:

- Moss query text.
- Returned Moss document IDs.
- External evidence IDs.
- Experiment run IDs.
- Critic report IDs.
- Retrieval timestamp.
- Active ResearchBrief version.

This answers: **What did the system know when it made this recommendation?**

---

## 11. Web Research Integration

### 11.1 Firecrawl

Use Firecrawl as the primary reader for selected public URLs.

Workflow:

```text
Search/discover source → Firecrawl scrape → clean Markdown/structured extraction
→ source quality review → EvidenceCard → Moss indexing
```

Use cases:

- Official Gymnasium, MuJoCo, Gymnasium-Robotics, and Stable-Baselines3 documentation.
- Benchmark/repository documentation.
- A small set of papers/preprints on robust RL, reward shaping, and safety constraints.

### 11.2 Bright Data

Use Bright Data narrowly:

- SERP API for current source discovery.
- Web Unlocker only for public pages that cannot be retrieved normally.
- Structured source discovery where an approved public source/API supports it.

Compliance rules:

- No login bypass.
- No paywall bypass.
- No private data collection.
- No scraping beyond site terms, permissions, rate limits, or relevant law.

### 11.3 Source quality gate

Each candidate source receives:

```json
{
  "evidence_level": "official_docs | peer_reviewed | preprint | technical_blog",
  "relevance": "high | medium | low",
  "approved_for": "method_design_only",
  "claim_status": "external_prior_not_local_result"
}
```

---

## 12. Experiment Execution

### 12.1 Recommended software stack

| Layer | Tool |
|---|---|
| Environment | Gymnasium with MuJoCo; start with `Reacher` or `Pendulum` |
| RL algorithm | Stable-Baselines3 PPO |
| Training utilities | Optional RL Baselines3 Zoo |
| Data/experiment truth | SQLite or DuckDB |
| Retrieval | Moss |
| Web ingestion | Firecrawl; Bright Data for discovery/fallback |
| UI | Streamlit |
| Voice narration | ElevenLabs |

### 12.2 Runner interface

```python
run_id = run_experiment(
    proposal_id="EXP-PROP-012",
    approved_config={
        "environment": "Reacher-v5",
        "algorithm": "PPO",
        "noise_train": 0.05,
        "noise_eval": 0.05,
        "safety_penalty": 0.5,
        "seed": 19,
        "training_steps": 100000,
        "evaluation_episodes": 100
    }
)
```

### 12.3 Required runner outputs

- Exact effective config.
- Environment/package versions.
- Seed.
- Training duration.
- Metrics under clean, noisy, and safety evaluation.
- Training log/learning curve.
- Model checkpoint (optional for MVP).
- Render/video artifact (optional for demo).
- Error trace when failed.

### 12.4 Initial campaign

| Run | Condition | Purpose |
|---|---|---|
| EXP-001 | Baseline: default training, seed 7 | Establish reference |
| EXP-002 | Baseline evaluated with observation noise | Measure baseline fragility |
| EXP-003 | Train with noise 0.05, seed 7 | Initial robustness test |
| EXP-004 | Train with noise 0.05, seed 19 | Replication |
| EXP-005 | Train with noise 0.05, seed 42 | Replication |
| EXP-006 | Add safety penalty, selected seed(s) | Map safety-performance tradeoff |
| EXP-007 | Combined noise + safety variant | Test bounded combined intervention |

### 12.5 Experiment summary template for Moss

```text
Document type: experiment_summary
Experiment: EXP-003
ResearchBrief version: 1
Environment: Reacher-v5
Algorithm: PPO
Hypothesis: Observation noise σ=0.05 during training improves noisy-evaluation
success compared with baseline.

Protocol:
- Training steps: 100,000
- Seed: 7
- Training observation noise: 0.05
- Evaluation observation noise: 0.05
- PPO architecture and evaluation protocol unchanged

Results:
- Clean success: [value]
- Noisy success: [value]
- Baseline noisy success: [value]
- Safety violations/episode: [value]

Status:
Single-seed result only. Marked provisional pending seeds 19 and 42.
```

---

## 13. Critic and Coordinator Logic

### 13.1 Critic checks

The critic must inspect:

- Same environment and wrapper versions?
- Same algorithm and policy architecture?
- Same training-step budget?
- Same evaluation episode count and noise protocol?
- At least three seeds for a non-exploratory claim?
- Safety metric available and comparable?
- Any failed/interrupted runs excluded with an explanation?
- Does reward move independently from actual task success?

### 13.2 Verdict rules

| Condition | Verdict |
|---|---|
| Inconsistent controls/evaluation | `invalid_comparison` |
| One seed or insufficient replication | `provisional` |
| Metrics fail success criteria | `inconclusive` |
| Replicated improvement, controls valid | `supported` |

### 13.3 Next-experiment scoring

Use transparent deterministic ranking before LLM explanation:

\[
\text{priority}(e) =
0.4 \cdot \text{uncertainty reduction}
+ 0.3 \cdot \text{expected robustness value}
+ 0.2 \cdot \text{safety value}
- 0.1 \cdot \text{compute cost}
\]

MVP rules:

- Prefer replication if a promising result has fewer than three seeds.
- Prefer one-factor tests before multi-factor combinations.
- Do not propose values outside allowed ranges.
- Prioritize safety investigation if violations exceed a ResearchBrief threshold.
- Pause if needed metrics are missing or a comparison is invalid.

---

## 14. Streamlit UI Specification

### 14.1 Pages

1. **Project Overview**
   - Active ResearchBrief, version, question, budget, constraints.
   - Research status: active / waiting for decision / paused.

2. **Scientist Inputs**
   - Submit question, hypothesis, constraints, notes.
   - Upload CSV/JSON/Markdown.
   - View data validation and provenance.

3. **Evidence → Experiment**
   - Evidence card.
   - Scope/limitation.
   - Derived hypothesis.
   - Proposed protocol.
   - Completed local result.

4. **Decision Center**
   - Pending DecisionRecords.
   - Option cards with rationale, evidence, risk, cost.
   - `Approve`, `Edit`, `Reject`, `Modify target`, `Custom direction`, `Not deciding yet`.

5. **Experiment Queue and Results**
   - Status table: draft/proposed/approved/running/completed.
   - Learning curves, metric comparisons, seed summary.
   - Artifact/video links where available.

6. **Lab Notebook**
   - Chronological audit events.
   - Human edits/rejections and agent recommendations.
   - ResearchBrief version history.

7. **Evidence Memory**
   - Moss search box.
   - Retrieved experiment reports, critic notes, external evidence, decision records.

8. **Lab Director**
   - Evidence-grounded short summary.
   - Optional ElevenLabs audio playback.

### 14.2 Decision card example

```text
Decision needed: What should Scientesis investigate next?

Recommended — Replicate the noise-training result
Why: EXP-003 is a single-seed positive finding; replication is required before
claiming a robustness improvement.
Evidence: EXP-001, EXP-003, CRITIC-001
Benefit: Highest uncertainty reduction.
Risk: Uses 2 experiment slots.
Cost: 200,000 training steps.

Alternative — Test safety penalty = 0.50
Why: The noise-trained policy showed elevated hazard entries.
Evidence: EXP-003
Benefit: Maps safety-performance tradeoff.
Risk: Adds another variable before replication.
Cost: 100,000 training steps.

Alternative — Modify target
Change metric, allowed range, budget, environment, or research goal.

[Choose replication] [Choose safety test] [Modify target]
[Enter a custom direction] [Not deciding yet]
```

---

## 15. ElevenLabs Integration

### 15.1 Purpose

ElevenLabs improves accessibility and demo clarity. It should narrate **validated** scientific state rather than act as the research engine.

### 15.2 Allowed narration inputs

- Stored exact metrics.
- Critic verdict.
- Human interpretation status.
- Authorized next action.
- Existing source/experiment IDs.

### 15.3 Example narrated summary

```text
Experiment EXP-003 is complete. Under noisy evaluation, success rose from
43 percent in the baseline to 57 percent in the candidate condition. This
finding is provisional because it comes from one training seed. Safety
violations were 0.18 per episode. The approved next action is replication
with seeds 19 and 42 before any general claim is made.
```

### 15.4 MVP feature

Add a `Hear lab summary` button to the results page. Generate audio only on completed experiments, decision outcomes, or direct user request—never on every training log update.

---

## 16. API and Validation Contracts

### 16.1 Experiment proposal validation

```python
def validate_experiment_proposal(proposal, research_brief, approvals):
    assert proposal.status in {"draft", "proposed", "reviewed"}
    assert proposal.config["environment"] == research_brief.approved_environment
    assert proposal.config["algorithm"] == research_brief.approved_algorithm
    assert proposal.config["noise_train"] in research_brief.allowed_noise_values
    assert proposal.config["safety_penalty"] in research_brief.allowed_safety_penalties
    assert proposal.config["training_steps"] <= research_brief.max_training_steps_per_run
    assert approvals.covers(proposal.config)
    return True
```

### 16.2 Approval check before execution

```python
def can_execute(proposal, active_brief, approval):
    if proposal.status != "approved":
        return False, "Proposal is not approved"
    if proposal.research_brief_version != active_brief.version:
        return False, "ResearchBrief changed; proposal requires revalidation"
    if not approval.scope_covers(proposal.config):
        return False, "Approval scope does not cover requested configuration"
    return True, None
```

### 16.3 ResearchBrief modification

A Brief version increments only when a human-approved edit changes:

- Research question.
- Primary/secondary outcomes.
- Constraints/parameter ranges.
- Budget.
- Validity rules.
- Authorized environment/algorithm.

### 16.4 Result-to-narration safety

```python
def build_narration(run_metrics, critic_report, human_interpretation):
    assert run_metrics is not None
    assert critic_report.verdict in {
        "supported", "provisional", "inconclusive", "invalid_comparison"
    }
    return render_factual_template(run_metrics, critic_report, human_interpretation)
```

---

## 17. Repository Structure

```text
scientesis/
├── README.md
├── requirements.txt
├── app.py                         # Streamlit entry point
├── configs/
│   ├── research_brief.example.json
│   ├── baseline.yaml
│   ├── noise_train_005.yaml
│   └── safety_penalty_050.yaml
├── scientesis/
│   ├── db/
│   │   ├── schema.sql
│   │   ├── repository.py
│   │   └── migrations.py
│   ├── domain/
│   │   ├── models.py
│   │   ├── research_brief.py
│   │   ├── decision_record.py
│   │   ├── experiment.py
│   │   └── evidence.py
│   ├── services/
│   │   ├── decision_service.py
│   │   ├── approval_service.py
│   │   ├── preference_service.py
│   │   ├── validation_service.py
│   │   └── audit_service.py
│   ├── agents/
│   │   ├── librarian.py
│   │   ├── idea_agent.py
│   │   ├── critic.py
│   │   ├── coordinator.py
│   │   └── lab_director.py
│   ├── retrieval/
│   │   ├── moss_client.py
│   │   ├── firecrawl_client.py
│   │   ├── brightdata_client.py
│   │   └── evidence_indexer.py
│   ├── rl/
│   │   ├── runner.py
│   │   ├── environment_factory.py
│   │   ├── wrappers.py
│   │   ├── evaluation.py
│   │   └── metrics.py
│   ├── ui/
│   │   ├── overview.py
│   │   ├── inputs.py
│   │   ├── decision_center.py
│   │   ├── experiments.py
│   │   ├── notebook.py
│   │   └── lab_director.py
│   └── audio/
│       └── elevenlabs_client.py
├── artifacts/
│   ├── models/
│   ├── plots/
│   ├── videos/
│   └── logs/
└── tests/
    ├── test_decision_service.py
    ├── test_approval_service.py
    ├── test_experiment_validation.py
    ├── test_wrappers.py
    └── test_critic_rules.py
```

---

## 18. Implementation Plan

### Phase 0: skeleton and data model

- Create repository and SQLite schema.
- Implement domain models for ResearchBrief, DecisionRecord, ExperimentProposal, ExperimentRun.
- Add audit events.
- Seed a sample project and brief.

**Done when:** the UI can display `ResearchBrief v1` and persist a basic project.

### Phase 1: runnable scientific baseline

- Set up Gymnasium and Stable-Baselines3.
- Start with `Pendulum` if `Reacher` setup blocks progress; migrate once stable.
- Run PPO baseline with a fixed seed.
- Save config, versions, metrics, and artifacts.

**Done when:** `run_experiment(config)` produces a stored run and visible metrics.

### Phase 2: controlled interventions

- Add observation-noise wrapper.
- Add hazard-zone/safety-cost wrapper.
- Add evaluation under clean/noisy conditions.
- Run baseline plus one noise intervention.

**Done when:** a comparison table contains at least two real runs.

### Phase 3: human control plane

- Build ResearchBrief form/versioning.
- Build hypothesis editor.
- Build DecisionRecord creation and option-card UI.
- Add approve/edit/reject/defer flow.
- Enforce approval check in runner.

**Done when:** no experiment can execute without valid approval coverage.

### Phase 4: critic and coordinator

- Implement deterministic validity checks.
- Build critic report generation.
- Implement simple next-experiment priority heuristic.
- Support pending-decision branch blocking.

**Done when:** one completed run leads to a critic verdict and either an authorized next action or a decision card.

### Phase 5: retrieval and literature evidence

- Add Firecrawl ingestion for 3–5 curated public sources.
- Add Moss indexing for evidence cards, experiment summaries, and decision records.
- Add evidence snapshot persistence.
- Add Bright Data SERP only if needed for source discovery.

**Done when:** the planner can display evidence IDs and source limitations that motivated a proposal.

### Phase 6: presentation polish

- Add Streamlit charts, notebook timeline, and evidence-to-experiment panel.
- Add ElevenLabs narrated summary.
- Prepare precomputed results, a simulation recording, and a demo scenario.

**Done when:** a viewer can understand question → decision → experiment → result → critique → next step in under two minutes.

---

## 19. Team Assignment (Four People)

| Person | Primary responsibility | Secondary responsibility |
|---|---|---|
| A | Gymnasium/SB3 pipeline, training, evaluation, artifacts | Simulation video/demo |
| B | Environment wrappers, metrics, config validation, tests | Dataset profiling/provenance |
| C | Data model, Decision Service, Moss, Firecrawl/Bright Data | Planner/critic integration |
| D | Streamlit UI, decision cards, notebook, ElevenLabs, pitch | Integration and demo orchestration |

For a smaller team, prioritize in this order:

1. Runnable baseline and stored results.
2. Baseline plus one controlled intervention.
3. ResearchBrief plus human approval gate.
4. Critic verdict plus next-decision card.
5. Retrieval/Moss.
6. Voice and visual polish.

---

## 20. Definition of Done

Scientesis is ready to demo when it can demonstrate all of the following:

- A human-authored, versioned research question and constraint set.
- A scientist-written or scientist-edited falsifiable hypothesis.
- A real baseline and at least two controlled simulation conditions.
- Persisted configurations, seeds, package/environment versions, metrics, and artifacts.
- A DecisionRecord that stores options, rationale, evidence snapshot, selection, optional human reason, scope, and ResearchBrief versions.
- An explicit human approval gate before execution.
- No inference of consent from a missing answer.
- A critic that flags at least one limitation, uncertainty, confounder, or replication need.
- A coordinator that proposes an evidence-backed next experiment within approved bounds.
- Moss retrieval of prior experiment notes, human constraints, and/or external evidence cards.
- Clear separation between literature-based plausibility and local experimental findings.
- A restrained final conclusion, such as:

> In this simulated reaching environment and fixed PPO budget, training with moderate observation noise showed a preliminary robustness effect. The result remains contingent on replication across seeds and does not establish a general robotics result or real-world deployment performance.

---

## 21. Demo Script

1. Open **Project Overview** and show `ResearchBrief v1`:
   - Question: robust robot learning under noisy sensing.
   - Constraints: max noise 0.05, fixed budget, three-seed requirement.

2. Show an **Evidence → Experiment** card:
   - External prior says noise augmentation may improve robustness.
   - Limitation says it is not proof for this environment.

3. Use the **Hypothesis Editor**:
   - Scientist edits the noise ceiling and accepts the protocol.

4. Show a **DecisionRecord**:
   - Planner recommends replication versus a safety-penalty test.
   - Explain both alternatives, cost, risks, and evidence.
   - Scientist selects replication and writes a reason.

5. Show a completed **Experiment Result**:
   - Baseline and candidate metrics.
   - Critic labels the first result provisional due to one seed.

6. Show the **Lab Notebook**:
   - Scientist decision, experiment run, critic report, and next action all have versioned records.

7. Play the **Lab Director** summary:

```text
The first noise-trained policy improved noisy-condition performance, but the
result is preliminary. Scientesis did not overclaim. It recorded the scientist's
replication decision, queued only the authorized seeds, and preserved the full
evidence trail for review.
```

---

## 22. One-Minute Pitch

> Scientesis is a human-guided agentic research lab for robust robot learning. Scientists define the question, contribute data, edit hypotheses, set constraints, and approve experiments. AI agents retrieve relevant evidence, transform scientific intent into controlled protocols, run reproducible simulations, audit results for uncertainty and safety, and recommend the next highest-value experiment. Scientesis never treats an AI preference—or a scientist’s silence—as authorization. Every decision stores its options, supporting evidence snapshot, scope, and ResearchBrief version, creating a reproducible trail from question to conclusion. Moss provides persistent evidence memory, Firecrawl and Bright Data support traceable research ingestion, and ElevenLabs makes validated scientific reasoning accessible through a Lab Director voice.
