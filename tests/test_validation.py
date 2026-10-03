import unittest

from scientesis.services.validation import (
    ProtocolValidationError,
    validate_config,
    validate_execution_authorization,
)

BRIEF = {
    "version": 1,
    "environment": "Reacher-v5",
    "algorithm": "PPO",
    "allowed_noise_values": [0.0, 0.02, 0.05],
    "allowed_safety_penalties": [0.0, 0.25, 0.5, 1.0],
    "training_seeds": [7, 19, 42],
    "training_steps_per_run": 100000,
    "evaluation_episodes": 100,
    "success_distance_threshold": 0.05,
    "actuator_saturation_threshold": 0.95,
}

CONFIG = {
    "environment": "Reacher-v5",
    "algorithm": "PPO",
    "noise_train": 0.05,
    "noise_eval": 0.05,
    "safety_penalty": 0.0,
    "seed": 7,
    "training_steps": 100000,
    "evaluation_episodes": 100,
    "success_distance_threshold": 0.05,
    "actuator_saturation_threshold": 0.95,
    "brief_version": 1,
}


class ValidationTests(unittest.TestCase):
    def test_accepts_config_inside_brief(self):
        validate_config(CONFIG, BRIEF)

    def test_rejects_unapproved_noise(self):
        config = dict(CONFIG, noise_train=0.1)
        with self.assertRaises(ProtocolValidationError):
            validate_config(config, BRIEF)

    def test_rejects_stale_brief_version(self):
        config = dict(CONFIG, brief_version=2)
        with self.assertRaises(ProtocolValidationError):
            validate_config(config, BRIEF)

    def test_rejects_missing_approval(self):
        proposal = {
            "status": "proposed",
            "research_brief_version": 1,
            "config": CONFIG,
            "approval_scope": None,
        }
        with self.assertRaises(ProtocolValidationError):
            validate_execution_authorization(proposal, BRIEF)

    def test_approval_must_cover_exact_config(self):
        proposal = {
            "status": "approved",
            "research_brief_version": 1,
            "config": CONFIG,
            "approval_scope": dict(CONFIG, noise_train=0.0),
        }
        with self.assertRaises(ProtocolValidationError):
            validate_execution_authorization(proposal, BRIEF)

    def test_accepts_exact_approval(self):
        proposal = {
            "status": "approved",
            "research_brief_version": 1,
            "config": CONFIG,
            "approval_scope": dict(CONFIG),
        }
        validate_execution_authorization(proposal, BRIEF)


if __name__ == "__main__":
    unittest.main()
