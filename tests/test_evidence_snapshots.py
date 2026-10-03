import copy
import sqlite3

import pytest

from scientesis.db.repository import Repository, json_text, new_id, utc_now
from scientesis.domain.models import ExperimentConfig
from scientesis.services.evidence import save_evidence_candidate
from scientesis.adapters.firecrawl import ScrapedPage
from scientesis.services.evidence_snapshots import build_moss_documents, snapshot_references_for_hits


def add_evidence(repository, project_id, root, status="approved"):
    page = ScrapedPage(
        source_url="https://example.org/research",
        title="Noise robustness methods",
        description="Public method notes.",
        markdown="Observation perturbation can improve robustness in some simulated tasks.",
        retrieved_at=utc_now(),
        status_code=200,
    )
    card = save_evidence_candidate(
        repository,
        project_id,
        {
            "title": "Noise robustness methods",
            "evidence_level": "peer_reviewed",
            "relevance": "high",
            "claim": "Observation perturbation may improve robustness.",
            "scope": "Simulated continuous-control environments.",
            "limitations": "Does not establish results for our task.",
            "implementation_hint": "Motivates the controlled comparison.",
        },
        page,
        root,
    )
    if status != "pending_review":
        repository.review_evidence_card(card["id"], status)
    return card


def add_completed_run_and_critic(repository, project_id):
    brief = repository.get_active_brief(project_id)
    config = ExperimentConfig.from_brief(brief, 0.0, 0.05, 0.0, 7).to_dict()
    proposal_id = repository.create_proposal(config, project_id)
    repository.approve_proposal(proposal_id)
    run_id = repository.reserve_run(proposal_id)
    repository.start_run(run_id)
    repository.complete_run(
        run_id,
        {"clean": {"success_rate": 0.7}, "noisy": {"success_rate": 0.6}},
        {"model_path": "artifacts/models/test.zip", "log_path": "artifacts/logs/test.json"},
        {"python": "3.12", "gymnasium": "1.3.0", "stable_baselines3": "2.9.0"},
    )
    report = {
        "id": new_id("CRITIC"),
        "project_id": project_id,
        "experiment_run_ids": [run_id],
        "verdict": "inconclusive",
        "findings": ["One run is insufficient for a claim."],
        "limitations": ["Only one seed."],
        "recommended_next_action": {"action": "replicate"},
        "created_at": utc_now(),
    }
    repository.save_critic_report(report)
    return run_id, report["id"]


def test_snapshot_freezes_approved_sources_runs_reports_and_active_brief(tmp_path):
    repository = Repository(tmp_path / "test.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    card = add_evidence(repository, project_id, tmp_path)
    run_id, report_id = add_completed_run_and_critic(repository, project_id)

    retrieved_hits = [
        {
            "id": "moss-doc-1",
            "score": 0.91,
            "text": "Approved external evidence about noise robustness.",
            "metadata": {
                "project_id": project_id,
                "record_type": "external_evidence",
                "evidence_id": card["id"],
                "source_url": card["source_url"],
            },
        },
        {
            "id": "moss-doc-2",
            "score": 0.83,
            "text": "The critic found one run insufficient.",
            "metadata": {
                "project_id": project_id,
                "record_type": "critic_report",
                "critic_report_id": report_id,
                "experiment_run_ids_json": f'["{run_id}"]',
            },
        },
    ]
    snapshot = repository.create_evidence_snapshot(
        project_id,
        moss_query="observation noise robustness",
        moss_document_ids=["moss-doc-1", "moss-doc-2"],
        retrieved_hits=retrieved_hits,
        source_ids=[card["id"]],
        critic_report_ids=[report_id],
    )

    assert snapshot["research_brief_version"] == 1
    assert snapshot["moss_query"] == "observation noise robustness"
    assert snapshot["moss_document_ids"] == ["moss-doc-1", "moss-doc-2"]
    assert [hit["id"] for hit in snapshot["retrieved_hits"]] == ["moss-doc-1", "moss-doc-2"]
    assert [hit["score"] for hit in snapshot["retrieved_hits"]] == [0.91, 0.83]
    assert all(hit["text"] for hit in snapshot["retrieved_hits"])
    assert snapshot["retrieved_hits"][0]["metadata"]["evidence_id"] == card["id"]
    assert snapshot["source_ids"] == [card["id"]]
    assert snapshot["experiment_ids"] == [run_id]
    assert snapshot["critic_report_ids"] == [report_id]
    assert snapshot["created_at"]
    assert any(event["entity_id"] == snapshot["id"] for event in repository.list_audit_events(project_id))

    with repository.connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE evidence_snapshots SET moss_query = 'changed' WHERE id = ?", (snapshot["id"],))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM evidence_snapshots WHERE id = ?", (snapshot["id"],))


def test_only_approved_project_sources_and_finished_project_runs_enter_a_snapshot(tmp_path):
    repository = Repository(tmp_path / "test.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    with pytest.raises(ValueError, match="at least one approved source"):
        repository.create_evidence_snapshot(project_id)
    pending = add_evidence(repository, project_id, tmp_path, "pending_review")

    with pytest.raises(ValueError, match="approved evidence"):
        repository.create_evidence_snapshot(project_id, source_ids=[pending["id"]])
    with pytest.raises(ValueError, match="belong to this project"):
        repository.create_evidence_snapshot(project_id, experiment_ids=["missing-run"])
    brief = repository.get_active_brief(project_id)
    config = ExperimentConfig.from_brief(brief, 0.0, 0.05, 0.0, 7).to_dict()
    proposal_id = repository.create_proposal(config, project_id)
    repository.approve_proposal(proposal_id)
    running_id = repository.reserve_run(proposal_id)
    repository.start_run(running_id)
    with pytest.raises(ValueError, match="completed or failed"):
        repository.create_evidence_snapshot(project_id, experiment_ids=[running_id])
    with pytest.raises(ValueError, match="query is required"):
        repository.create_evidence_snapshot(project_id, moss_document_ids=["doc-1"])
    with pytest.raises(ValueError, match="duplicate"):
        repository.create_evidence_snapshot(project_id, source_ids=["EV-1", "EV-1"])


def test_snapshot_links_must_match_project_and_active_brief_version(tmp_path):
    repository = Repository(tmp_path / "test.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    card = add_evidence(repository, project_id, tmp_path)
    snapshot = repository.create_evidence_snapshot(project_id, source_ids=[card["id"]], context_note="Current brief")
    brief = repository.get_active_brief(project_id)
    config = ExperimentConfig.from_brief(brief, 0.0, 0.05, 0.0, 7).to_dict()

    proposal_id = repository.create_proposal(config, project_id, evidence_snapshot_id=snapshot["id"])
    assert repository.get_proposal(proposal_id)["evidence_snapshot_id"] == snapshot["id"]
    decision_options = [
        {
            "label": label,
            "rationale": f"Rationale for {label}.",
            "benefits": ["Preserves provenance"],
            "risks": ["Uses historical context"],
            "cost_estimate": {"experiments": 0, "training_steps": 0},
            "evidence_refs": [],
            "is_recommended": False,
            "is_modify_target_option": label == "Review evidence",
        }
        for label in ("Review evidence", "Defer")
    ]
    decision_id = repository.create_decision(
        "Which source should inform the next method decision?",
        "research_direction",
        decision_options,
        project_id,
        evidence_snapshot_id=snapshot["id"],
    )
    assert repository.get_decision(decision_id)["evidence_snapshot_id"] == snapshot["id"]

    changed = copy.deepcopy(brief)
    changed["research_question"] = "Does the replicated policy retain success under sensor noise?"
    repository.update_research_brief(changed, "Tighten the research question", project_id)
    current_config = ExperimentConfig.from_brief(repository.get_active_brief(project_id), 0.0, 0.05, 0.0, 7).to_dict()
    with pytest.raises(ValueError, match="different ResearchBrief version"):
        repository.create_proposal(current_config, project_id, evidence_snapshot_id=snapshot["id"])
    with pytest.raises(ValueError, match="different ResearchBrief version"):
        repository.create_decision(
            "Use the old search context?",
            "research_direction",
            decision_options,
            project_id,
            evidence_snapshot_id=snapshot["id"],
        )


def test_snapshot_cannot_be_linked_to_a_different_project(tmp_path):
    repository = Repository(tmp_path / "test.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    brief = repository.get_active_brief(project_id)
    card = add_evidence(repository, project_id, tmp_path)
    snapshot = repository.create_evidence_snapshot(project_id, source_ids=[card["id"]], context_note="Current project scope")
    other_project_id = "separate-research-project"
    other_brief = copy.deepcopy(brief)
    other_brief["project_title"] = "Separate research project"
    with repository.connect() as connection:
        connection.execute(
            "INSERT INTO research_projects (id, title, created_by, created_at) VALUES (?, ?, ?, ?)",
            (other_project_id, other_brief["project_title"], "test", utc_now()),
        )
        connection.execute(
            "INSERT INTO research_brief_versions (id, project_id, version, brief_json, created_by, change_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (new_id("BRIEF"), other_project_id, 1, json_text(other_brief), "test", "Test project", utc_now()),
        )
    config = ExperimentConfig.from_brief(other_brief, 0.0, 0.05, 0.0, 7).to_dict()
    with pytest.raises(ValueError, match="must belong to this project"):
        repository.create_proposal(config, other_project_id, evidence_snapshot_id=snapshot["id"])


def test_corpus_builder_indexes_only_citable_project_records(tmp_path):
    repository = Repository(tmp_path / "test.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    approved = add_evidence(repository, project_id, tmp_path)
    add_evidence(repository, project_id, tmp_path, "pending_review")
    protocol = {
        "environment": "Reacher-v5",
        "algorithm": "PPO",
        "treatment": "Train with observation noise 0.05.",
        "control": "Train with observation noise 0.00.",
        "primary_outcome": "Success rate under evaluation noise 0.05.",
        "seed_protocol": [7, 19, 42],
    }
    hypothesis_id = repository.create_hypothesis("Noise training improves robustness.", protocol, project_id)
    repository.review_hypothesis(hypothesis_id, "approved")
    run_id, report_id = add_completed_run_and_critic(repository, project_id)
    documents = build_moss_documents(repository, project_id, tmp_path)
    by_type = {document["metadata"]["record_type"] for document in documents}

    assert {"research_brief", "approved_hypothesis", "experiment_summary", "critic_report", "external_evidence"} <= by_type
    assert all(document["metadata"]["project_id"] == project_id for document in documents)
    assert all(len(document["text"]) <= 16_000 for document in documents)
    evidence_documents = [document for document in documents if document["metadata"].get("evidence_id")]
    assert [document["metadata"]["evidence_id"] for document in evidence_documents] == [approved["id"]]
    assert any(document["metadata"].get("experiment_run_id") == run_id for document in documents)
    assert any(document["metadata"].get("critic_report_id") == report_id for document in documents)
    assert not any(document["metadata"].get("evidence_id") and document["metadata"]["evidence_id"] != approved["id"] for document in documents)


def test_retrieval_hits_map_back_to_exact_snapshot_references():
    documents = [
        {"id": "source-doc", "metadata": {"evidence_id": "EV-1", "project_id": "p"}},
        {"id": "critic-doc", "metadata": {"critic_report_id": "CR-1", "experiment_run_ids_json": '["RUN-1"]', "project_id": "p"}},
        {"id": "run-doc", "metadata": {"experiment_run_id": "RUN-2", "project_id": "p"}},
    ]
    hits = [{"id": document["id"], "score": 0.8, "text": "result"} for document in documents]
    refs = snapshot_references_for_hits(hits, ["critic-doc", "source-doc", "unknown"], documents)

    assert refs == {
        "moss_document_ids": ["critic-doc", "source-doc"],
        "source_ids": ["EV-1"],
        "experiment_ids": ["RUN-1"],
        "critic_report_ids": ["CR-1"],
    }

def test_legacy_database_migration_preserves_existing_evidence_and_snapshots(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE evidence_cards (
                id TEXT PRIMARY KEY, project_id TEXT, title TEXT, source_url TEXT, source_type TEXT,
                claim_text TEXT, scope_text TEXT, limitations_text TEXT, implementation_hint TEXT,
                retrieved_at TEXT, approved_by TEXT, moss_document_id TEXT
            );
            CREATE TABLE evidence_snapshots (
                id TEXT PRIMARY KEY, project_id TEXT, moss_query TEXT, source_ids_json TEXT,
                experiment_ids_json TEXT, critic_report_ids_json TEXT, created_at TEXT
            );
            CREATE TABLE experiment_proposals (
                id TEXT PRIMARY KEY, project_id TEXT, status TEXT
            );
            """
        )

    now = utc_now()
    project_id = "robust-robot-learning"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO evidence_cards (id, project_id, title, source_url, source_type, claim_text, scope_text, limitations_text, implementation_hint, retrieved_at, approved_by, moss_document_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("EV-legacy", project_id, "Legacy source", "https://example.org", "peer_reviewed", "Claim", "Scope", "Limits", "Use", now, "scientist", "moss-legacy"),
        )
        connection.execute(
            "INSERT INTO evidence_snapshots (id, project_id, moss_query, source_ids_json, experiment_ids_json, critic_report_ids_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("SNAP-legacy", project_id, "legacy query", '["EV-legacy"]', "[]", "[]", now),
        )

    repository = Repository(database)
    repository.initialize()
    card = repository.get_evidence_card("EV-legacy")
    snapshot = repository.get_evidence_snapshot("SNAP-legacy")
    assert card["approval_status"] == "approved"
    assert card["quality"] == {}
    assert snapshot["research_brief_version"] == 1
    assert snapshot["moss_document_ids"] == []
    assert snapshot["retrieved_hits"] == []
    assert snapshot["source_ids"] == ["EV-legacy"]
    assert snapshot["context_note"] is None
