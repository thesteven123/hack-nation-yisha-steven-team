import copy

import pytest

from scientesis.db.repository import Repository
from scientesis.domain.models import ExperimentConfig


def valid_hypothesis():
    return {
        "environment": "Reacher-v5",
        "algorithm": "PPO",
        "treatment": "Train with observation noise 0.05.",
        "control": "Train with observation noise 0.00.",
        "primary_outcome": "Success rate under evaluation noise 0.05.",
        "seed_protocol": [7, 19, 42],
    }


def test_brief_edits_create_immutable_versions_and_stale_proposals_cannot_run(tmp_path):
    repository = Repository(tmp_path / "test.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    brief = repository.get_active_brief(project_id)
    config = ExperimentConfig.from_brief(brief, 0.05, 0.05, 0.0, 7).to_dict()
    proposal_id = repository.create_proposal(config, project_id)
    repository.approve_proposal(proposal_id)

    changed = copy.deepcopy(brief)
    changed["research_question"] = "Does noise training improve performance across three independent seeds?"
    updated = repository.update_research_brief(changed, "Clarified the replication target.", project_id)

    assert updated["version"] == 2
    assert repository.get_active_brief(project_id)["research_question"] == changed["research_question"]
    versions = repository.list_brief_versions(project_id)
    assert [version["version"] for version in versions] == [2, 1]
    assert versions[1]["brief"]["research_question"] == brief["research_question"]
    with pytest.raises(ValueError, match="changed"):
        repository.reserve_run(proposal_id)


def test_brief_rejects_unsupported_and_unbounded_changes(tmp_path):
    repository = Repository(tmp_path / "test.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    brief = repository.get_active_brief(project_id)

    unsupported = copy.deepcopy(brief)
    unsupported["environment"] = "Pendulum-v1"
    with pytest.raises(ValueError, match="fixed"):
        repository.update_research_brief(unsupported, "Try a different task.", project_id)

    excessive = copy.deepcopy(brief)
    excessive["training_steps_per_run"] = excessive["max_training_steps_per_run"] + 1
    with pytest.raises(ValueError, match="Training steps"):
        repository.update_research_brief(excessive, "Raise the budget.", project_id)

    with pytest.raises(ValueError, match="change reason"):
        repository.update_research_brief(brief, "", project_id)


def test_hypothesis_requires_human_review_before_linking_to_a_proposal(tmp_path):
    repository = Repository(tmp_path / "test.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    brief = repository.get_active_brief(project_id)
    hypothesis_id = repository.create_hypothesis(
        "Training-time observation noise improves noisy-condition reaching success.",
        valid_hypothesis(),
        project_id,
    )
    hypothesis = repository.get_hypothesis(hypothesis_id)
    assert hypothesis["status"] == "draft"
    assert hypothesis["protocol"]["environment"] == "Reacher-v5"

    config = ExperimentConfig.from_brief(brief, 0.05, 0.05, 0.0, 7).to_dict()
    with pytest.raises(ValueError, match="approved hypothesis"):
        repository.create_proposal(config, project_id, hypothesis_id)

    repository.review_hypothesis(hypothesis_id, "approved", "Protocol matches the current brief.")
    proposal_id = repository.create_proposal(config, project_id, hypothesis_id)
    assert repository.get_proposal(proposal_id)["hypothesis_id"] == hypothesis_id


def test_invalid_hypotheses_and_duplicate_review_are_rejected(tmp_path):
    repository = Repository(tmp_path / "test.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    with pytest.raises(ValueError, match="missing required fields"):
        repository.create_hypothesis("A hypothesis.", {"environment": "Reacher-v5"}, project_id)

    hypothesis_id = repository.create_hypothesis("A valid hypothesis.", valid_hypothesis(), project_id)
    repository.review_hypothesis(hypothesis_id, "rejected", "Needs a narrower outcome.")
    with pytest.raises(ValueError, match="Only a draft"):
        repository.review_hypothesis(hypothesis_id, "approved")
