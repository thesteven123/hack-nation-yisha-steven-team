from scientesis.db.repository import Repository


def test_database_bootstrap_and_human_approval_gate(tmp_path):
    repo = Repository(tmp_path / "test.sqlite3")
    repo.initialize()
    brief = repo.get_active_brief()
    config = {
        "environment": brief["environment"],
        "algorithm": brief["algorithm"],
        "noise_train": 0.05,
        "noise_eval": 0.05,
        "safety_penalty": 0.0,
        "seed": 7,
        "training_steps": brief["training_steps_per_run"],
        "evaluation_episodes": brief["evaluation_episodes"],
        "success_distance_threshold": brief["success_distance_threshold"],
        "actuator_saturation_threshold": brief["actuator_saturation_threshold"],
        "brief_version": brief["version"],
    }
    proposal_id = repo.create_proposal(config)
    assert repo.get_proposal(proposal_id)["status"] == "proposed"
    repo.approve_proposal(proposal_id, "Human approval for this exact config")
    assert repo.get_proposal(proposal_id)["status"] == "approved"
    run_id = repo.reserve_run(proposal_id)
    run = repo.start_run(run_id)
    assert run["config"] == config
    assert repo.get_run(run_id)["status"] == "running"
