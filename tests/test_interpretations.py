import pytest

from scientesis.db.repository import Repository, utc_now
from scientesis.services.interpretations import validate_human_interpretation


def add_critic_report(repository, project_id):
    repository.save_critic_report(
        {
            "id": "CRITIC-TEST",
            "project_id": project_id,
            "experiment_run_ids": [],
            "verdict": "inconclusive",
            "findings": ["Insufficient replicated runs."],
            "limitations": ["Only one training seed."],
            "recommended_next_action": {"reason": "Replicate before claiming."},
            "created_at": utc_now(),
        }
    )


def test_human_interpretation_is_separate_and_append_only(tmp_path):
    repository = Repository(tmp_path / "test.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    add_critic_report(repository, project_id)

    first_id = repository.save_human_interpretation(
        "CRITIC-TEST", "preliminary", "Promising, but replication is still needed.", project_id
    )
    second_id = repository.save_human_interpretation(
        "CRITIC-TEST", "accepted_evidence", "Accept for this bounded simulator only.", project_id
    )

    [latest, previous] = repository.list_human_interpretations(project_id, "CRITIC-TEST")
    assert latest["id"] == second_id
    assert previous["id"] == first_id
    assert latest["verdict"] == "accepted_evidence"
    assert repository.list_critic_reports(project_id)[0]["verdict"] == "inconclusive"
    assert any(
        event["entity_id"] == second_id and event["action"] == "recorded"
        for event in repository.list_audit_events(project_id)
    )


def test_interpretation_requires_a_valid_verdict_and_existing_project_report(tmp_path):
    repository = Repository(tmp_path / "test.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()

    with pytest.raises(ValueError, match="Choose"):
        validate_human_interpretation("supported")
    with pytest.raises(ValueError, match="5,000"):
        validate_human_interpretation("preliminary", "x" * 5001)
    with pytest.raises(KeyError, match="Unknown critic report"):
        repository.save_human_interpretation("MISSING", "inconclusive", project_id=project_id)
