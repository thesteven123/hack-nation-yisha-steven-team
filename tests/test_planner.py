from copy import deepcopy

import pytest

from scientesis.db.repository import Repository, utc_now
from scientesis.domain.models import ExperimentConfig
from scientesis.services.planner import plan_next_work


def _complete_run(repository, brief, project_id, noise_train, noise_eval, seed):
    config = ExperimentConfig.from_brief(brief, noise_train, noise_eval, 0.0, seed).to_dict()
    proposal_id = repository.create_proposal(config, project_id)
    repository.approve_proposal(proposal_id)
    run_id = repository.reserve_run(proposal_id)
    repository.start_run(run_id)
    repository.complete_run(
        run_id,
        {
            "clean_success_rate": 0.5,
            "noisy_success_rate": 0.4,
            "clean_safety_violations_per_episode": 0.0,
            "noisy_safety_violations_per_episode": 0.0,
            "clean": {"success_rate": 0.5, "safety_violations_per_episode": 0.0},
            "noisy": {"success_rate": 0.4, "safety_violations_per_episode": 0.0},
        },
        {"model_path": f"/tmp/{run_id}.zip", "algorithm": "PPO"},
        {"gymnasium": "1.2.0", "stable_baselines3": "2.7.0"},
    )
    return run_id


def _seed_baseline(repository, project_id, brief):
    evaluation_noise = min(value for value in brief["allowed_noise_values"] if value > 0)
    run_ids = [
        _complete_run(repository, brief, project_id, 0.0, evaluation_noise, seed)
        for seed in brief["training_seeds"][: brief["minimum_seeds_for_claim"]]
    ]
    return run_ids, evaluation_noise


def test_planner_recommends_a_valid_baseline_without_saving_or_approving_anything(tmp_path):
    repository = Repository(tmp_path / "planner.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()

    plan = plan_next_work(repository, project_id)

    assert plan["status"] == "establish_baseline"
    assert plan["action"] == "establish_baseline"
    assert plan["config"]["noise_train"] == 0.0
    assert plan["config"]["noise_eval"] in repository.get_active_brief(project_id)["allowed_noise_values"]
    assert plan["config"]["seed"] in repository.get_active_brief(project_id)["training_seeds"]
    assert plan["priority"]["score"] == round(
        0.4 * plan["priority"]["uncertainty_reduction"]
        + 0.3 * plan["priority"]["robustness_value"]
        + 0.2 * plan["priority"]["safety_value"]
        - 0.1 * plan["priority"]["compute_cost"],
        3,
    )
    assert repository.list_runs(project_id) == []
    assert repository.list_proposals(project_id) == []
    assert repository.list_hypotheses(project_id) == []


def test_saved_planner_output_is_inert_and_requires_hypothesis_review(tmp_path):
    repository = Repository(tmp_path / "planner.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    brief = repository.get_active_brief(project_id)
    _seed_baseline(repository, project_id, brief)

    plan = plan_next_work(repository, project_id)
    assert plan["status"] == "test_training_noise"

    saved = repository.save_planner_draft(plan, project_id)
    proposal = repository.get_proposal(saved["proposal_id"])
    hypothesis = repository.get_hypothesis(saved["hypothesis_id"])
    assert proposal["status"] == "proposed"
    assert proposal["hypothesis_id"] == hypothesis["id"]
    assert hypothesis["status"] == "draft"
    assert hypothesis["source"] == "deterministic_planner"
    assert repository.list_runs(project_id) == [
        run for run in repository.list_runs(project_id) if run["status"] == "completed"
    ]
    with pytest.raises(ValueError, match="Review and approve the linked hypothesis"):
        repository.approve_proposal(proposal["id"])

    repository.review_hypothesis(hypothesis["id"], "approved", "The protocol matches the active brief.")
    repository.approve_proposal(proposal["id"])
    assert repository.get_proposal(proposal["id"])["status"] == "approved"
    assert len(repository.list_runs(project_id)) == len(brief["training_seeds"][: brief["minimum_seeds_for_claim"]])


def test_planner_draft_rejects_stale_brief_and_out_of_scope_config(tmp_path):
    repository = Repository(tmp_path / "planner.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    plan = plan_next_work(repository, project_id)
    changed = deepcopy(repository.get_active_brief(project_id))
    changed["notes"] = changed["notes"] + " Clarified after planning."
    repository.update_research_brief(changed, "Clarify plan scope.", project_id)
    with pytest.raises(ValueError, match="ResearchBrief changed after planning"):
        repository.save_planner_draft(plan, project_id)
    assert repository.list_proposals(project_id) == []

def test_pending_decision_does_not_block_independent_baseline_work(tmp_path):
    repository = Repository(tmp_path / "planner.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    repository.create_decision(
        "Which later intervention should be tested?",
        "research_direction",
        [
            {
                "label": label,
                "rationale": f"Reason for {label}.",
                "benefits": ["Records the option"],
                "risks": ["Requires review"],
                "cost_estimate": {"experiments": 0},
                "evidence_refs": [],
                "is_recommended": False,
                "is_modify_target_option": label == "Modify the ResearchBrief target",
            }
            for label in ["Modify the ResearchBrief target", "Keep the current target"]
        ],
        project_id,
    )

    plan = plan_next_work(repository, project_id)

    assert plan["status"] == "establish_baseline"
    assert len(plan["context"]["pending_decisions"]) == 1
    assert repository.list_decisions(project_id)[0]["status"] == "pending"


def test_open_proposal_is_returned_instead_of_creating_a_duplicate(tmp_path):
    repository = Repository(tmp_path / "planner.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    brief = repository.get_active_brief(project_id)
    config = ExperimentConfig.from_brief(brief, 0.0, 0.05, 0.0, brief["training_seeds"][0]).to_dict()
    proposal_id = repository.create_proposal(config, project_id)

    plan = plan_next_work(repository, project_id)

    assert plan["status"] == "existing_work"
    assert plan["proposal_id"] == proposal_id
    assert len(repository.list_proposals(project_id)) == 1


def test_planner_pairs_first_intervention_with_completed_baseline_seeds(tmp_path):
    repository = Repository(tmp_path / "planner.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    brief = repository.get_active_brief(project_id)
    baseline_ids, evaluation_noise = _seed_baseline(repository, project_id, brief)

    plan = plan_next_work(repository, project_id)

    assert plan["status"] == "test_training_noise"
    assert plan["config"]["noise_train"] == min(value for value in brief["allowed_noise_values"] if value > 0)
    assert plan["config"]["noise_eval"] == evaluation_noise
    assert plan["config"]["seed"] == brief["training_seeds"][0]
    assert plan["context"]["paired_seed_set"] == brief["training_seeds"][: brief["minimum_seeds_for_claim"]]
    assert plan["hypothesis"]["protocol"]["seed_protocol"] == plan["context"]["paired_seed_set"]
    assert all(repository.get_run(run_id)["status"] == "completed" for run_id in baseline_ids)
    assert len(repository.list_proposals(project_id)) == len(baseline_ids)


def test_scope_exhaustion_yields_a_valid_decision_draft_not_an_out_of_scope_config(tmp_path):
    repository = Repository(tmp_path / "planner.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    brief = repository.get_active_brief(project_id)
    candidate = deepcopy(brief)
    candidate["allowed_noise_values"] = [0.0]
    repository.update_research_brief(candidate, "Keep the current scope to the baseline.", project_id)
    updated = repository.get_active_brief(project_id)
    for seed in updated["training_seeds"][: updated["minimum_seeds_for_claim"]]:
        _complete_run(repository, updated, project_id, 0.0, 0.0, seed)

    plan = plan_next_work(repository, project_id)

    assert plan["status"] == "decision_required"
    assert plan["decision_draft"]["decision_type"] == "research_direction"
    assert len(plan["decision_draft"]["options"]) in {2, 3, 4}
    assert all(option["is_recommended"] is False for option in plan["decision_draft"]["options"])
    assert repository.list_decisions(project_id) == []


def test_planner_requests_critic_then_scientist_interpretation(tmp_path):
    repository = Repository(tmp_path / "planner.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    brief = repository.get_active_brief(project_id)
    baseline_ids, evaluation_noise = _seed_baseline(repository, project_id, brief)
    treatment_ids = [
        _complete_run(repository, brief, project_id, min(value for value in brief["allowed_noise_values"] if value > 0), evaluation_noise, seed)
        for seed in brief["training_seeds"][: brief["minimum_seeds_for_claim"]]
    ]

    plan = plan_next_work(repository, project_id)
    assert plan["status"] == "critic_required"
    assert plan["run_id"] == treatment_ids[-1]

    report_id = f"CRIT-PLAN-{brief['version']}"
    repository.save_critic_report(
        {
            "id": report_id,
            "project_id": project_id,
            "experiment_run_ids": baseline_ids + treatment_ids,
            "verdict": "supported",
            "findings": ["Synthetic fixture for planner state coverage."],
            "limitations": ["This test does not run a model."],
            "recommended_next_action": {"action": "human_interpretation"},
            "created_by": "test",
            "created_at": utc_now(),
        }
    )

    plan = plan_next_work(repository, project_id)
    assert plan["status"] == "human_interpretation_required"
    assert plan["critic_report_id"] == report_id
    assert repository.list_human_interpretations(project_id) == []
def test_saving_baseline_plan_creates_only_an_unapproved_proposal(tmp_path):
    repository = Repository(tmp_path / "planner.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    plan = plan_next_work(repository, project_id)
    saved = repository.save_planner_draft(plan, project_id)
    proposal = repository.get_proposal(saved["proposal_id"])
    assert proposal["status"] == "proposed"
    assert proposal["proposed_by"] == "deterministic_planner"
    assert saved["hypothesis_id"] is None
    assert repository.list_runs(project_id) == []
    with pytest.raises(ValueError, match="equivalent proposal already exists"):
        repository.save_planner_draft(plan, project_id)


def test_planner_save_revalidates_the_generated_action_and_matched_protocol(tmp_path):
    repository = Repository(tmp_path / "planner.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    plan = plan_next_work(repository, project_id)
    plan["config"]["noise_train"] = 0.05

    with pytest.raises(ValueError, match="baseline planner draft must use zero training noise"):
        repository.save_planner_draft(plan, project_id)

    assert repository.list_proposals(project_id) == []
    assert repository.list_hypotheses(project_id) == []

def test_saving_intervention_plan_keeps_hypothesis_and_proposal_unapproved(tmp_path):
    repository = Repository(tmp_path / "planner.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    brief = repository.get_active_brief(project_id)
    _seed_baseline(repository, project_id, brief)
    plan = plan_next_work(repository, project_id)
    assert plan["action"] == "test_training_noise"
    saved = repository.save_planner_draft(plan, project_id)
    hypothesis = repository.get_hypothesis(saved["hypothesis_id"])
    proposal = repository.get_proposal(saved["proposal_id"])
    assert hypothesis["status"] == "draft"
    assert hypothesis["source"] == "deterministic_planner"
    assert proposal["status"] == "proposed"
    with pytest.raises(ValueError, match="Review and approve the linked hypothesis"):
        repository.approve_proposal(saved["proposal_id"])
    repository.review_hypothesis(saved["hypothesis_id"], "approved", "Protocol reviewed by the scientist.")
    repository.approve_proposal(saved["proposal_id"])
    assert repository.get_proposal(saved["proposal_id"])["status"] == "approved"
    assert len(repository.list_runs(project_id)) == brief["minimum_seeds_for_claim"]

def test_stale_planner_output_cannot_be_saved_after_brief_changes(tmp_path):
    repository = Repository(tmp_path / "planner.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    plan = plan_next_work(repository, project_id)
    brief = deepcopy(repository.get_active_brief(project_id))
    brief["notes"] = "Updated after planner output was created."
    repository.update_research_brief(brief, "Clarify the active research scope.", project_id)
    with pytest.raises(ValueError, match="ResearchBrief changed after planning"):
        repository.save_planner_draft(plan, project_id)
