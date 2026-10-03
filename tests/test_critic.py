from scientesis.services.critic import _same_variant


def test_condition_match_ignores_only_seed():
    config = {
        "environment": "Reacher-v5",
        "algorithm": "PPO",
        "noise_train": 0.05,
        "noise_eval": 0.05,
        "safety_penalty": 0.0,
        "training_steps": 100000,
        "evaluation_episodes": 100,
        "brief_version": 1,
        "success_distance_threshold": 0.05,
        "actuator_saturation_threshold": 0.95,
        "seed": 7,
    }
    same = dict(config, seed=19)
    different = dict(config, safety_penalty=0.5)
    assert _same_variant(config, same)
    assert not _same_variant(config, different)
