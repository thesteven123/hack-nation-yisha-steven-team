PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS research_projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_brief_versions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES research_projects(id),
    version INTEGER NOT NULL,
    brief_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    change_reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, version)
);

CREATE TABLE IF NOT EXISTS hypotheses (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES research_projects(id),
    original_text TEXT NOT NULL,
    operationalized_protocol_json TEXT,
    brief_version INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    reviewed_by TEXT,
    review_note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES research_projects(id),
    name TEXT NOT NULL,
    storage_uri TEXT NOT NULL,
    source_type TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    validation_report_json TEXT,
    sha256 TEXT,
    uploaded_by TEXT NOT NULL,
    uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_cards (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES research_projects(id),
    title TEXT NOT NULL,
    source_url TEXT,
    source_type TEXT NOT NULL,
    claim_text TEXT NOT NULL,
    scope_text TEXT,
    limitations_text TEXT,
    implementation_hint TEXT,
    retrieved_at TEXT NOT NULL,
    approved_by TEXT,
    moss_document_id TEXT
);

CREATE TABLE IF NOT EXISTS evidence_snapshots (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES research_projects(id),
    moss_query TEXT,
    source_ids_json TEXT NOT NULL,
    experiment_ids_json TEXT NOT NULL,
    critic_report_ids_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decision_records (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES research_projects(id),
    question TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence_snapshot_id TEXT REFERENCES evidence_snapshots(id),
    pre_brief_version INTEGER NOT NULL,
    post_brief_version INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    answered_by TEXT,
    answered_at TEXT
);

CREATE TABLE IF NOT EXISTS decision_options (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES decision_records(id),
    label TEXT NOT NULL,
    rationale TEXT NOT NULL,
    benefits_json TEXT,
    risks_json TEXT,
    cost_estimate_json TEXT,
    evidence_refs_json TEXT NOT NULL,
    is_recommended INTEGER NOT NULL DEFAULT 0,
    is_modify_target_option INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS decision_answers (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES decision_records(id),
    answer_type TEXT NOT NULL,
    selected_option_id TEXT REFERENCES decision_options(id),
    custom_response TEXT,
    rationale_optional TEXT,
    scope_json TEXT NOT NULL,
    preference_source TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preference_records (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES research_projects(id),
    statement TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL,
    scope_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiment_proposals (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES research_projects(id),
    hypothesis_id TEXT REFERENCES hypotheses(id),
    research_brief_version INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL,
    proposed_by TEXT NOT NULL,
    approval_scope_json TEXT,
    approved_by TEXT,
    approval_note TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT
);

CREATE TABLE IF NOT EXISTS experiment_runs (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES experiment_proposals(id),
    project_id TEXT NOT NULL REFERENCES research_projects(id),
    status TEXT NOT NULL,
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

CREATE TABLE IF NOT EXISTS metric_records (
    id TEXT PRIMARY KEY,
    experiment_run_id TEXT NOT NULL REFERENCES experiment_runs(id),
    metric_name TEXT NOT NULL,
    metric_value REAL NOT NULL,
    condition_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS critic_reports (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES research_projects(id),
    experiment_run_ids_json TEXT NOT NULL,
    verdict TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    limitations_json TEXT,
    recommended_next_action_json TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS human_interpretations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES research_projects(id),
    critic_report_id TEXT NOT NULL REFERENCES critic_reports(id),
    verdict TEXT NOT NULL,
    rationale TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES research_projects(id),
    experiment_run_id TEXT NOT NULL REFERENCES experiment_runs(id),
    artifact_type TEXT NOT NULL,
    storage_uri TEXT NOT NULL,
    sha256 TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES research_projects(id),
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    payload_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_brief_project_version ON research_brief_versions(project_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_proposals_project_status ON experiment_proposals(project_id, status);
CREATE INDEX IF NOT EXISTS idx_runs_project_status ON experiment_runs(project_id, status);
CREATE INDEX IF NOT EXISTS idx_audit_project_created ON audit_events(project_id, created_at DESC);
