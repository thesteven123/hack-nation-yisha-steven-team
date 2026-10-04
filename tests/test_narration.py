import copy
import hashlib
import json
import math
import sqlite3
from types import SimpleNamespace

import pytest

from scientesis.db.repository import Repository, utc_now
from scientesis.services.narration import build_lab_summary, create_narration_audio, load_narration_audio


@pytest.fixture
def lab(tmp_path):
    repository = Repository(tmp_path / "lab.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    brief = repository.get_active_brief(project_id)
    config = {
        "environment": brief["environment"], "algorithm": brief["algorithm"],
        "noise_train": 0.02, "noise_eval": 0.02, "safety_penalty": 0.0, "seed": 7,
        "training_steps": brief["training_steps_per_run"], "evaluation_episodes": brief["evaluation_episodes"],
        "success_distance_threshold": brief["success_distance_threshold"],
        "actuator_saturation_threshold": brief["actuator_saturation_threshold"], "brief_version": brief["version"],
    }
    proposal_id = repository.create_proposal(config, project_id)
    repository.approve_proposal(proposal_id)
    run_id = repository.reserve_run(proposal_id)
    repository.start_run(run_id)
    metrics = {
        "clean_success_rate": 0.57, "noisy_success_rate": 0.43,
        "clean_safety_violations_per_episode": 0.18, "noisy_safety_violations_per_episode": 0.28,
    }
    repository.complete_run(run_id, metrics, {"model_path": "artifacts/models/test.zip"}, {})
    repository.save_critic_report({
        "id": "CRIT-NARRATION", "project_id": project_id, "experiment_run_ids": [run_id],
        "verdict": "provisional", "findings": ["UNTRUSTED CLAIM"], "limitations": ["One seed"],
        "recommended_next_action": {"action": "replicate", "reason": "UNTRUSTED INSTRUCTION"}, "created_at": utc_now(),
    })
    return repository, project_id, run_id, config


class FakeVoice:
    settings = SimpleNamespace(voice_id="test-voice", model_id="test-model", api_key="never-save-this-key")

    def __init__(self, callback=None):
        self.calls = []
        self.callback = callback

    def synthesize(self, text):
        self.calls.append(text)
        if self.callback:
            self.callback()
        return b"ID3test-mocked-audio"


def test_empty_summary_is_read_only_and_makes_no_finding(tmp_path):
    repository = Repository(tmp_path / "empty.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    before = repository.list_audit_events(project_id)
    summary = build_lab_summary(repository, project_id)
    assert "No result or research finding" in summary["text"]
    assert summary["state"]["run"] is None
    assert "not evidence of physical robot safety" in summary["text"]
    assert repository.list_audit_events(project_id) == before
    with pytest.raises(ValueError, match="completed run"):
        build_lab_summary(repository, project_id, "MISSING")


def test_summary_keeps_exact_metrics_critic_and_human_separate(lab):
    repository, project_id, run_id, _ = lab
    repository.save_human_interpretation("CRIT-NARRATION", "accepted_evidence", "UNTRUSTED HUMAN CLAIM", project_id)
    summary = build_lab_summary(repository, project_id, run_id)
    text = summary["text"]
    assert "57 percent" in text and "43 percent" in text and "0.18" in text and "0.28" in text
    assert "1 distinct stored training seeds" in text and "requires 3 for a claim" in text
    assert "provisional verdict" in text and "accepted evidence" in text
    assert "not a pooled comparison" in text and "not permission" in text
    assert "UNTRUSTED" not in text
    assert summary["fingerprint"] == build_lab_summary(repository, project_id, run_id)["fingerprint"]


@pytest.mark.parametrize("value", [True, math.nan, -0.1, 1.1, None])
def test_invalid_metrics_are_not_narrated(lab, value):
    repository, project_id, run_id, _ = lab
    metrics = repository.get_run(run_id)["metrics"]
    metrics["noisy_success_rate"] = value
    with repository.connect() as connection:
        connection.execute("UPDATE experiment_runs SET metrics_json = ? WHERE id = ?", (json.dumps(metrics), run_id))
    with pytest.raises(ValueError, match="missing or invalid"):
        build_lab_summary(repository, project_id, run_id)


def test_nested_metrics_must_agree(lab):
    repository, project_id, run_id, _ = lab
    metrics = repository.get_run(run_id)["metrics"]
    metrics["noisy"] = {"success_rate": 0.9}
    with repository.connect() as connection:
        connection.execute("UPDATE experiment_runs SET metrics_json = ? WHERE id = ?", (json.dumps(metrics), run_id))
    with pytest.raises(ValueError, match="disagrees"):
        build_lab_summary(repository, project_id, run_id)


def test_approval_is_exact_and_old_briefs_are_not_current_permission(lab):
    repository, project_id, run_id, config = lab
    proposal_id = repository.create_proposal(dict(config, seed=19), project_id)
    assert build_lab_summary(repository, project_id, run_id)["state"]["approved_proposal"] is None
    repository.approve_proposal(proposal_id)
    assert build_lab_summary(repository, project_id, run_id)["state"]["approved_proposal"]["id"] == proposal_id
    brief = copy.deepcopy(repository.get_active_brief(project_id))
    brief["notes"] += " Clarified the scope."
    repository.update_research_brief(brief, "Scientist clarification", project_id)
    summary = build_lab_summary(repository, project_id, run_id)
    assert summary["state"]["approved_proposal"] is None
    assert "older version 1" in summary["text"]


def test_audio_is_persisted_audited_and_does_not_authorize_work(lab, tmp_path):
    repository, project_id, run_id, _ = lab
    client = FakeVoice()
    before = repository.list_proposals(project_id), repository.list_runs(project_id)
    summary = build_lab_summary(repository, project_id, run_id)
    manifest = create_narration_audio(client, repository, project_id, run_id, summary["fingerprint"], tmp_path)
    assert client.calls == [summary["text"]]
    assert load_narration_audio(manifest, tmp_path) == b"ID3test-mocked-audio"
    assert manifest["audio_sha256"] == hashlib.sha256(b"ID3test-mocked-audio").hexdigest()
    assert repository.list_narration_artifacts(project_id, run_id) == [manifest]
    assert (repository.list_proposals(project_id), repository.list_runs(project_id)) == before
    assert "never-save-this-key" not in json.dumps(manifest)
    event = next(event for event in repository.list_audit_events(project_id) if event["entity_id"] == manifest["id"])
    assert event["action"] == "audio_generated_from_captured_state"
    assert event["payload"]["critic_report_id"] == "CRIT-NARRATION"
    audio_path = tmp_path / manifest["audio_path"]
    audio_path.write_bytes(b"ID3tampered")
    with pytest.raises(ValueError, match="integrity"):
        load_narration_audio(manifest, tmp_path)
    with pytest.raises(ValueError, match="audio folder"):
        load_narration_audio(dict(manifest, audio_path="../outside.mp3"), tmp_path)


@pytest.mark.parametrize("during_request", [False, True])
def test_stale_state_never_saves_audio(lab, tmp_path, during_request):
    repository, project_id, run_id, config = lab
    proposal_id = repository.create_proposal(dict(config, seed=19), project_id)
    summary = build_lab_summary(repository, project_id, run_id)
    callback = lambda: repository.approve_proposal(proposal_id)
    client = FakeVoice(callback if during_request else None)
    if not during_request:
        callback()
    with pytest.raises(ValueError, match="research state changed"):
        create_narration_audio(client, repository, project_id, run_id, summary["fingerprint"], tmp_path)
    assert len(client.calls) == int(during_request)
    assert repository.list_narration_artifacts(project_id) == []
    assert not (tmp_path / "artifacts" / "audio").exists()


def test_database_failure_removes_partial_artifacts(lab, tmp_path, monkeypatch):
    repository, project_id, run_id, _ = lab
    def fail_record(*_args):
        raise sqlite3.OperationalError("Simulated database failure")
    monkeypatch.setattr(repository, "record_narration_artifact", fail_record)
    summary = build_lab_summary(repository, project_id, run_id)
    with pytest.raises(sqlite3.OperationalError):
        create_narration_audio(FakeVoice(), repository, project_id, run_id, summary["fingerprint"], tmp_path)
    assert list((tmp_path / "artifacts" / "audio").iterdir()) == []


def test_symlinked_audio_folder_cannot_escape_project(lab, tmp_path):
    repository, project_id, run_id, _ = lab
    root = tmp_path / "project"
    (root / "artifacts").mkdir(parents=True)
    (root / "artifacts" / "audio").symlink_to(tmp_path, target_is_directory=True)
    client = FakeVoice()
    summary = build_lab_summary(repository, project_id, run_id)
    with pytest.raises(ValueError, match="inside the project"):
        create_narration_audio(client, repository, project_id, run_id, summary["fingerprint"], root)
    assert client.calls == []
