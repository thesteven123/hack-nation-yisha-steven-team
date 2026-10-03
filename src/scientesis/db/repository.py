import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from scientesis.services.decision_records import validate_decision_draft
from scientesis.services.interpretations import validate_human_interpretation
from scientesis.services.hypotheses import validate_hypothesis
from scientesis.services.research_briefs import validate_research_brief
from scientesis.services.validation import validate_config, validate_execution_authorization

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "scientesis.sqlite3"
DEFAULT_BRIEF = PROJECT_ROOT / "configs" / "research_brief.example.json"
SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_text(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


class Repository:
    def __init__(self, database_path: str | Path | None = None):
        configured = database_path or os.environ.get("SCIENTESIS_DB_PATH") or DEFAULT_DATABASE
        self.database_path = Path(configured).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self):
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        schema = SCHEMA_FILE.read_text(encoding="utf-8")
        with self.connect() as connection:
            connection.executescript(schema)
            hypothesis_columns = {row["name"] for row in connection.execute("PRAGMA table_info(hypotheses)")}
            if "brief_version" not in hypothesis_columns:
                connection.execute("ALTER TABLE hypotheses ADD COLUMN brief_version INTEGER NOT NULL DEFAULT 1")
            existing = connection.execute("SELECT id FROM research_projects LIMIT 1").fetchone()
            if existing:
                return
            brief = json.loads(DEFAULT_BRIEF.read_text(encoding="utf-8"))
            brief["version"] = 1
            project_id = "robust-robot-learning"
            now = utc_now()
            connection.execute(
                "INSERT INTO research_projects (id, title, created_by, created_at) VALUES (?, ?, ?, ?)",
                (project_id, brief["project_title"], "system_seed", now),
            )
            connection.execute(
                "INSERT INTO research_brief_versions (id, project_id, version, brief_json, created_by, change_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new_id("BRIEF"), project_id, 1, json_text(brief), "system", "Initial MVP brief", now),
            )
            self._audit(
                connection,
                project_id,
                "research_project",
                project_id,
                "seeded_initial_project",
                "system",
                {"brief_version": 1},
            )

    def get_project_id(self) -> str:
        with self.connect() as connection:
            row = connection.execute("SELECT id FROM research_projects ORDER BY created_at LIMIT 1").fetchone()
        if row is None:
            raise RuntimeError("No project is initialized. Call initialize() first.")
        return row["id"]

    def get_project(self, project_id: str | None = None) -> dict:
        project_id = project_id or self.get_project_id()
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM research_projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown project: {project_id}")
        return dict(row)

    def get_active_brief(self, project_id: str | None = None) -> dict:
        project_id = project_id or self.get_project_id()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT brief_json FROM research_brief_versions WHERE project_id = ? ORDER BY version DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"Project {project_id} has no ResearchBrief.")
        return json.loads(row["brief_json"])

    def list_brief_versions(self, project_id: str | None = None) -> list[dict]:
        project_id = project_id or self.get_project_id()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT version, brief_json, created_by, change_reason, created_at FROM research_brief_versions WHERE project_id = ? ORDER BY version DESC",
                (project_id,),
            ).fetchall()
        versions = []
        for row in rows:
            version = dict(row)
            version["brief"] = json.loads(version.pop("brief_json"))
            versions.append(version)
        return versions

    def update_research_brief(
        self,
        candidate: dict,
        reason: str,
        project_id: str | None = None,
    ) -> dict:
        project_id = project_id or self.get_project_id()
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 1000:
            raise ValueError("Record a change reason of 1–1,000 characters.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            project = connection.execute("SELECT id FROM research_projects WHERE id = ?", (project_id,)).fetchone()
            if project is None:
                raise KeyError(f"Unknown project: {project_id}")
            current = self._get_active_brief(connection, project_id)
            runs_used = connection.execute(
                "SELECT COUNT(*) AS total FROM experiment_runs WHERE project_id = ?", (project_id,)
            ).fetchone()["total"]
            updated = validate_research_brief(current, candidate, runs_used)
            updated["version"] = current["version"] + 1
            changed_fields = sorted(
                field for field in updated if updated[field] != current.get(field)
            )
            now = utc_now()
            connection.execute(
                "INSERT INTO research_brief_versions (id, project_id, version, brief_json, created_by, change_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new_id("BRIEF"), project_id, updated["version"], json_text(updated), "scientist", reason.strip(), now),
            )
            connection.execute(
                "UPDATE research_projects SET title = ? WHERE id = ?",
                (updated["project_title"], project_id),
            )
            self._audit(
                connection,
                project_id,
                "research_brief",
                str(updated["version"]),
                "versioned_by_scientist",
                "human",
                {"from_version": current["version"], "to_version": updated["version"], "changed_fields": changed_fields, "reason": reason.strip()},
            )
        return updated

    def create_hypothesis(
        self,
        original_text: str,
        protocol: dict,
        project_id: str | None = None,
    ) -> str:
        project_id = project_id or self.get_project_id()
        normalized = validate_hypothesis(original_text, protocol)
        brief = self.get_active_brief(project_id)
        hypothesis_id = new_id("HYP")
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO hypotheses (id, project_id, original_text, operationalized_protocol_json, brief_version, source, status, created_by, created_at) VALUES (?, ?, ?, ?, ?, 'human', 'draft', 'scientist', ?)",
                (hypothesis_id, project_id, original_text.strip(), json_text(normalized), brief["version"], now),
            )
            self._audit(
                connection,
                project_id,
                "hypothesis",
                hypothesis_id,
                "created_draft",
                "human",
                {"brief_version": brief["version"], "original_text": original_text.strip()},
            )
        return hypothesis_id

    def get_hypothesis(self, hypothesis_id: str) -> dict:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM hypotheses WHERE id = ?", (hypothesis_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown hypothesis: {hypothesis_id}")
        hypothesis = dict(row)
        value = hypothesis.pop("operationalized_protocol_json")
        hypothesis["protocol"] = json.loads(value) if value else None
        return hypothesis

    def list_hypotheses(self, project_id: str | None = None, status: str | None = None) -> list[dict]:
        project_id = project_id or self.get_project_id()
        query = "SELECT id FROM hypotheses WHERE project_id = ?"
        values = [project_id]
        if status is not None:
            query += " AND status = ?"
            values.append(status)
        query += " ORDER BY created_at DESC, rowid DESC"
        with self.connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [self.get_hypothesis(row["id"]) for row in rows]

    def review_hypothesis(self, hypothesis_id: str, decision: str, note: str = "") -> None:
        if decision not in {"approved", "rejected"}:
            raise ValueError("Hypothesis review must be approved or rejected.")
        if not isinstance(note, str) or len(note) > 2000:
            raise ValueError("Keep the review note under 2,000 characters.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT project_id, status FROM hypotheses WHERE id = ?", (hypothesis_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown hypothesis: {hypothesis_id}")
            if row["status"] != "draft":
                raise ValueError("Only a draft hypothesis can be reviewed.")
            connection.execute(
                "UPDATE hypotheses SET status = ?, reviewed_by = 'scientist', review_note = ? WHERE id = ?",
                (decision, note.strip() or None, hypothesis_id),
            )
            self._audit(
                connection,
                row["project_id"],
                "hypothesis",
                hypothesis_id,
                decision,
                "human",
                {"note": note.strip() or None},
            )

    def create_proposal(
        self,
        config: dict,
        project_id: str | None = None,
        hypothesis_id: str | None = None,
    ) -> str:
        project_id = project_id or self.get_project_id()
        brief = self.get_active_brief(project_id)
        validate_config(config, brief)
        with self.connect() as connection:
            if hypothesis_id is not None:
                hypothesis = connection.execute(
                    "SELECT id, brief_version FROM hypotheses WHERE id = ? AND project_id = ? AND status = 'approved'",
                    (hypothesis_id, project_id),
                ).fetchone()
                if hypothesis is None:
                    raise ValueError("Only a scientist-approved hypothesis from this project can be linked.")
                if hypothesis["brief_version"] != brief["version"]:
                    raise ValueError("This hypothesis refers to an older ResearchBrief; create and approve a current one.")
            used = connection.execute(
                "SELECT COUNT(*) AS total FROM experiment_runs WHERE project_id = ?", (project_id,)
            ).fetchone()["total"]
            if used >= brief["experiment_budget"]:
                raise ValueError("The active ResearchBrief experiment budget has been reached.")
            proposal_id = new_id("PROP")
            connection.execute(
                "INSERT INTO experiment_proposals (id, project_id, hypothesis_id, research_brief_version, config_json, status, proposed_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (proposal_id, project_id, hypothesis_id, brief["version"], json_text(config), "proposed", "scientist", utc_now()),
            )
            self._audit(
                connection,
                project_id,
                "experiment_proposal",
                proposal_id,
                "proposed",
                "human",
                {"config": config, "hypothesis_id": hypothesis_id},
            )
        return proposal_id

    def approve_proposal(self, proposal_id: str, note: str = "") -> None:
        proposal = self.get_proposal(proposal_id)
        brief = self.get_active_brief(proposal["project_id"])
        if proposal["status"] not in {"proposed", "reviewed"}:
            raise ValueError("Only a proposed or reviewed experiment can be approved.")
        if proposal["research_brief_version"] != brief["version"]:
            raise ValueError("The ResearchBrief changed; create a fresh proposal before approving.")
        validate_config(proposal["config"], brief)
        with self.connect() as connection:
            connection.execute(
                "UPDATE experiment_proposals SET status = 'approved', approval_scope_json = ?, approved_by = ?, approval_note = ?, approved_at = ? WHERE id = ?",
                (json_text(proposal["config"]), "scientist", note, utc_now(), proposal_id),
            )
            self._audit(
                connection,
                proposal["project_id"],
                "experiment_proposal",
                proposal_id,
                "explicitly_approved",
                "human",
                {"scope": proposal["config"], "note": note},
            )

    def reserve_run(self, proposal_id: str) -> str:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM experiment_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown proposal: {proposal_id}")
            proposal = self._decode_proposal(row)
            if proposal["status"] != "approved":
                raise ValueError("Only a freshly approved proposal can reserve a run.")
            brief = self._get_active_brief(connection, proposal["project_id"])
            validate_execution_authorization(proposal, brief)
            used = connection.execute(
                "SELECT COUNT(*) AS total FROM experiment_runs WHERE project_id = ?",
                (proposal["project_id"],),
            ).fetchone()["total"]
            if used >= brief["experiment_budget"]:
                raise ValueError("The active ResearchBrief experiment budget has been reached.")
            run_id = new_id("EXP")
            now = utc_now()
            connection.execute(
                "INSERT INTO experiment_runs (id, proposal_id, project_id, status, seed, config_json) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, proposal_id, proposal["project_id"], "queued", proposal["config"]["seed"], json_text(proposal["config"])),
            )
            connection.execute(
                "UPDATE experiment_proposals SET status = 'queued' WHERE id = ?", (proposal_id,)
            )
            self._audit(
                connection,
                proposal["project_id"],
                "experiment_run",
                run_id,
                "queued_after_authorization_check",
                "system",
                {"proposal_id": proposal_id, "brief_version": brief["version"]},
            )
        return run_id

    def start_run(self, run_id: str) -> dict:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run_row = connection.execute("SELECT * FROM experiment_runs WHERE id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise KeyError(f"Unknown run: {run_id}")
            if run_row["status"] != "queued":
                raise ValueError(f"Run must be queued before starting; found {run_row['status']}.")
            proposal_row = connection.execute(
                "SELECT * FROM experiment_proposals WHERE id = ?", (run_row["proposal_id"],)
            ).fetchone()
            proposal = self._decode_proposal(proposal_row)
            brief = self._get_active_brief(connection, run_row["project_id"])
            validate_execution_authorization(proposal, brief)
            now = utc_now()
            connection.execute(
                "UPDATE experiment_runs SET status = 'running', started_at = ? WHERE id = ?",
                (now, run_id),
            )
            self._audit(
                connection,
                run_row["project_id"],
                "experiment_run",
                run_id,
                "started_after_preflight_authorization_check",
                "system",
                {"proposal_id": run_row["proposal_id"], "brief_version": brief["version"]},
            )
            return self._decode_run(connection.execute("SELECT * FROM experiment_runs WHERE id = ?", (run_id,)).fetchone())

    def complete_run(self, run_id: str, metrics: dict, artifact_manifest: dict, versions: dict) -> None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM experiment_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown run: {run_id}")
            if row["status"] != "running":
                raise ValueError("Only a running experiment can be completed.")
            connection.execute(
                "UPDATE experiment_runs SET status = 'completed', environment_version = ?, algorithm_version = ?, metrics_json = ?, artifact_manifest_json = ?, completed_at = ? WHERE id = ?",
                (versions.get("gymnasium"), versions.get("stable_baselines3"), json_text(metrics), json_text(artifact_manifest), utc_now(), run_id),
            )
            for condition in ("clean", "noisy"):
                block = metrics.get(condition, {})
                for metric_name, value in block.items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        connection.execute(
                            "INSERT INTO metric_records (id, experiment_run_id, metric_name, metric_value, condition_name, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                            (new_id("METRIC"), run_id, metric_name, float(value), condition, utc_now()),
                        )
            connection.execute(
                "INSERT INTO artifacts (id, project_id, experiment_run_id, artifact_type, storage_uri, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new_id("ARTIFACT"), row["project_id"], run_id, "model_checkpoint", artifact_manifest["model_path"], json_text(artifact_manifest), utc_now()),
            )
            self._audit(
                connection,
                row["project_id"],
                "experiment_run",
                run_id,
                "completed",
                "system",
                {"metrics": metrics, "versions": versions},
            )

    def fail_run(self, run_id: str, error_message: str) -> None:
        with self.connect() as connection:
            row = connection.execute("SELECT project_id FROM experiment_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown run: {run_id}")
            connection.execute(
                "UPDATE experiment_runs SET status = 'failed', error_message = ?, completed_at = ? WHERE id = ?",
                (error_message[:12000], utc_now(), run_id),
            )
            self._audit(connection, row["project_id"], "experiment_run", run_id, "failed", "system", {"error": error_message[:2000]})

    def get_proposal(self, proposal_id: str) -> dict:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM experiment_proposals WHERE id = ?", (proposal_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown proposal: {proposal_id}")
        return self._decode_proposal(row)

    def list_proposals(self, project_id: str | None = None) -> list[dict]:
        project_id = project_id or self.get_project_id()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM experiment_proposals WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        return [self._decode_proposal(row) for row in rows]

    def get_run(self, run_id: str) -> dict:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM experiment_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown run: {run_id}")
        return self._decode_run(row)

    def list_runs(self, project_id: str | None = None) -> list[dict]:
        project_id = project_id or self.get_project_id()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM experiment_runs WHERE project_id = ? ORDER BY COALESCE(started_at, id) DESC",
                (project_id,),
            ).fetchall()
        return [self._decode_run(row) for row in rows]

    def list_completed_runs(self, project_id: str | None = None) -> list[dict]:
        return [run for run in self.list_runs(project_id) if run["status"] == "completed"]

    def create_decision(
        self,
        question: str,
        decision_type: str,
        options: list[dict],
        project_id: str | None = None,
        evidence_snapshot_id: str | None = None,
    ) -> str:
        if not isinstance(decision_type, str) or not decision_type.strip() or len(decision_type.strip()) > 100:
            raise ValueError("Enter a decision type of 1–100 characters.")
        options = validate_decision_draft(question, options)
        project_id = project_id or self.get_project_id()
        brief = self.get_active_brief(project_id)
        decision_id = new_id("DR")
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO decision_records (id, project_id, question, decision_type, status, evidence_snapshot_id, pre_brief_version, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (decision_id, project_id, question.strip(), decision_type.strip(), "pending", evidence_snapshot_id, brief["version"], "scientist", now),
            )
            option_ids = []
            for option in options:
                option_id = new_id("OPT")
                option_ids.append(option_id)
                connection.execute(
                    "INSERT INTO decision_options (id, decision_id, label, rationale, benefits_json, risks_json, cost_estimate_json, evidence_refs_json, is_recommended, is_modify_target_option) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        option_id,
                        decision_id,
                        option["label"],
                        option["rationale"],
                        json_text(option["benefits"]),
                        json_text(option["risks"]),
                        json_text(option["cost_estimate"]),
                        json_text(option["evidence_refs"]),
                        int(option["is_recommended"]),
                        int(option["is_modify_target_option"]),
                    ),
                )
            self._audit(
                connection,
                project_id,
                "decision_record",
                decision_id,
                "created_pending",
                "human",
                {"question": question.strip(), "option_ids": option_ids, "pre_brief_version": brief["version"]},
            )
        return decision_id

    def get_decision(self, decision_id: str) -> dict:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM decision_records WHERE id = ?", (decision_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown decision: {decision_id}")
            options = connection.execute(
                "SELECT * FROM decision_options WHERE decision_id = ? ORDER BY rowid", (decision_id,)
            ).fetchall()
            answers = connection.execute(
                "SELECT * FROM decision_answers WHERE decision_id = ? ORDER BY created_at, rowid", (decision_id,)
            ).fetchall()
        decision = dict(row)
        decision["options"] = [self._decode_decision_option(option) for option in options]
        decision["answers"] = [self._decode_decision_answer(answer) for answer in answers]
        return decision

    def list_decisions(self, project_id: str | None = None) -> list[dict]:
        project_id = project_id or self.get_project_id()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM decision_records WHERE project_id = ? ORDER BY created_at DESC, rowid DESC",
                (project_id,),
            ).fetchall()
        return [self.get_decision(row["id"]) for row in rows]

    def answer_decision(
        self,
        decision_id: str,
        answer_type: str,
        scope: dict,
        selected_option_id: str | None = None,
        custom_response: str | None = None,
        rationale: str | None = None,
    ) -> str:
        if answer_type not in {"option_selected", "custom_text", "defer"}:
            raise ValueError("Answer type must be option_selected, custom_text, or defer.")
        if not isinstance(scope, dict) or not isinstance(scope.get("applies_to"), str) or not scope["applies_to"].strip():
            raise ValueError("State the scope of this answer.")
        if scope.get("creates_permanent_preference") is not False:
            raise ValueError("A one-time answer cannot silently become a permanent preference.")
        if answer_type == "option_selected":
            if not isinstance(selected_option_id, str) or not selected_option_id:
                raise ValueError("Select one of this decision's alternatives.")
            if custom_response and custom_response.strip():
                raise ValueError("An option selection cannot also contain a custom direction.")
        elif answer_type == "custom_text":
            if not isinstance(custom_response, str) or not custom_response.strip():
                raise ValueError("Enter a custom direction before recording it.")
            if len(custom_response.strip()) > 5000:
                raise ValueError("Keep the custom direction under 5,000 characters.")
            if selected_option_id:
                raise ValueError("A custom direction cannot also select an option.")
        elif selected_option_id or (isinstance(custom_response, str) and custom_response.strip()):
            raise ValueError("A deferral does not select an option or submit a custom direction.")
        if rationale is not None and (not isinstance(rationale, str) or len(rationale) > 5000):
            raise ValueError("Keep the rationale under 5,000 characters.")

        answer_id = new_id("ANS")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            decision = connection.execute(
                "SELECT project_id, status FROM decision_records WHERE id = ?", (decision_id,)
            ).fetchone()
            if decision is None:
                raise KeyError(f"Unknown decision: {decision_id}")
            if decision["status"] != "pending":
                raise ValueError("Only a pending decision can receive a new answer.")
            if selected_option_id:
                option = connection.execute(
                    "SELECT id FROM decision_options WHERE id = ? AND decision_id = ?",
                    (selected_option_id, decision_id),
                ).fetchone()
                if option is None:
                    raise ValueError("The selected option does not belong to this decision.")
            connection.execute(
                "INSERT INTO decision_answers (id, decision_id, answer_type, selected_option_id, custom_response, rationale_optional, scope_json, preference_source, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    answer_id,
                    decision_id,
                    answer_type,
                    selected_option_id if answer_type == "option_selected" else None,
                    custom_response.strip() if answer_type == "custom_text" else None,
                    rationale.strip() if rationale and rationale.strip() else None,
                    json_text(scope),
                    "explicit_human",
                    "scientist",
                    utc_now(),
                ),
            )
            if answer_type != "defer":
                connection.execute(
                    "UPDATE decision_records SET status = 'answered', answered_by = ?, answered_at = ? WHERE id = ?",
                    ("scientist", utc_now(), decision_id),
                )
            self._audit(
                connection,
                decision["project_id"],
                "decision_record",
                decision_id,
                "deferred" if answer_type == "defer" else "answered",
                "human",
                {"answer_id": answer_id, "answer_type": answer_type, "scope": scope},
            )
        return answer_id

    @staticmethod
    def _decode_decision_option(row) -> dict:
        option = dict(row)
        for field in ("benefits_json", "risks_json", "cost_estimate_json", "evidence_refs_json"):
            option[field.removesuffix("_json")] = json.loads(option.pop(field))
        option["is_recommended"] = bool(option["is_recommended"])
        option["is_modify_target_option"] = bool(option["is_modify_target_option"])
        return option

    @staticmethod
    def _decode_decision_answer(row) -> dict:
        answer = dict(row)
        answer["scope"] = json.loads(answer.pop("scope_json"))
        return answer

    def add_dataset(self, record: dict) -> None:
        required = {"id", "project_id", "name", "storage_uri", "source_type", "provenance", "validation_report", "sha256", "uploaded_by"}
        missing = sorted(required - set(record))
        if missing:
            raise ValueError(f"Dataset record is missing fields: {', '.join(missing)}")
        if record["source_type"] != "human_upload":
            raise ValueError("This intake path accepts human uploads only.")
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO datasets (id, project_id, name, storage_uri, source_type, provenance_json, validation_report_json, sha256, uploaded_by, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["id"],
                    record["project_id"],
                    record["name"],
                    record["storage_uri"],
                    record["source_type"],
                    json_text(record["provenance"]),
                    json_text(record["validation_report"]),
                    record["sha256"],
                    record["uploaded_by"],
                    utc_now(),
                ),
            )
            self._audit(
                connection,
                record["project_id"],
                "dataset",
                record["id"],
                "human_upload_saved_after_review",
                "human",
                {"name": record["name"], "storage_uri": record["storage_uri"], "sha256": record["sha256"]},
            )

    def list_datasets(self, project_id: str | None = None) -> list[dict]:
        project_id = project_id or self.get_project_id()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM datasets WHERE project_id = ? ORDER BY uploaded_at DESC",
                (project_id,),
            ).fetchall()
        datasets = []
        for row in rows:
            dataset = dict(row)
            dataset["provenance"] = json.loads(dataset.pop("provenance_json"))
            dataset["validation_report"] = json.loads(dataset.pop("validation_report_json"))
            datasets.append(dataset)
        return datasets

    def save_critic_report(self, report: dict) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO critic_reports (id, project_id, experiment_run_ids_json, verdict, findings_json, limitations_json, recommended_next_action_json, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report["id"],
                    report["project_id"],
                    json_text(report["experiment_run_ids"]),
                    report["verdict"],
                    json_text(report["findings"]),
                    json_text(report["limitations"]),
                    json_text(report["recommended_next_action"]),
                    "deterministic_critic",
                    report["created_at"],
                ),
            )
            self._audit(
                connection,
                report["project_id"],
                "critic_report",
                report["id"],
                "created",
                "system",
                {"verdict": report["verdict"], "experiment_run_ids": report["experiment_run_ids"]},
            )

    def list_critic_reports(self, project_id: str | None = None) -> list[dict]:
        project_id = project_id or self.get_project_id()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM critic_reports WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        reports = []
        for row in rows:
            report = dict(row)
            for field in ("experiment_run_ids_json", "findings_json", "limitations_json", "recommended_next_action_json"):
                value = report.pop(field)
                key = field.removesuffix("_json")
                report[key] = json.loads(value) if value else None
            reports.append(report)
        return reports

    def save_human_interpretation(
        self,
        critic_report_id: str,
        verdict: str,
        rationale: str | None = None,
        project_id: str | None = None,
    ) -> str:
        normalized = validate_human_interpretation(verdict, rationale)
        project_id = project_id or self.get_project_id()
        interpretation_id = new_id("INTERP")
        created_at = utc_now()
        with self.connect() as connection:
            report = connection.execute(
                "SELECT id FROM critic_reports WHERE id = ? AND project_id = ?",
                (critic_report_id, project_id),
            ).fetchone()
            if report is None:
                raise KeyError(f"Unknown critic report for this project: {critic_report_id}")
            connection.execute(
                "INSERT INTO human_interpretations (id, project_id, critic_report_id, verdict, rationale, created_by, created_at) VALUES (?, ?, ?, ?, ?, 'scientist', ?)",
                (interpretation_id, project_id, critic_report_id, normalized["verdict"], normalized["rationale"], created_at),
            )
            self._audit(
                connection,
                project_id,
                "human_interpretation",
                interpretation_id,
                "recorded",
                "human",
                {"critic_report_id": critic_report_id, **normalized},
            )
        return interpretation_id

    def list_human_interpretations(
        self,
        project_id: str | None = None,
        critic_report_id: str | None = None,
    ) -> list[dict]:
        project_id = project_id or self.get_project_id()
        query = "SELECT * FROM human_interpretations WHERE project_id = ?"
        values = [project_id]
        if critic_report_id is not None:
            query += " AND critic_report_id = ?"
            values.append(critic_report_id)
        query += " ORDER BY created_at DESC, rowid DESC"
        with self.connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [dict(row) for row in rows]

    def list_audit_events(self, project_id: str | None = None, limit: int = 100) -> list[dict]:
        project_id = project_id or self.get_project_id()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["payload"] = json.loads(event.pop("payload_json")) if event["payload_json"] else {}
            events.append(event)
        return events

    @staticmethod
    def _get_active_brief(connection, project_id: str) -> dict:
        row = connection.execute(
            "SELECT brief_json FROM research_brief_versions WHERE project_id = ? ORDER BY version DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Project {project_id} has no ResearchBrief.")
        return json.loads(row["brief_json"])

    @staticmethod
    def _decode_proposal(row) -> dict:
        proposal = dict(row)
        proposal["config"] = json.loads(proposal.pop("config_json"))
        scope = proposal.pop("approval_scope_json")
        proposal["approval_scope"] = json.loads(scope) if scope else None
        return proposal

    @staticmethod
    def _decode_run(row) -> dict:
        run = dict(row)
        for raw, decoded in (
            ("config_json", "config"),
            ("metrics_json", "metrics"),
            ("artifact_manifest_json", "artifact_manifest"),
        ):
            value = run.pop(raw)
            run[decoded] = json.loads(value) if value else None
        return run

    @staticmethod
    def _audit(connection, project_id: str, entity_type: str, entity_id: str, action: str, actor_type: str, payload: dict) -> None:
        connection.execute(
            "INSERT INTO audit_events (id, project_id, entity_type, entity_id, action, actor_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id("AUDIT"), project_id, entity_type, entity_id, action, actor_type, json_text(payload), utc_now()),
        )
