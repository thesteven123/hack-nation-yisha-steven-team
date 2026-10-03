import json
import platform
import time
from importlib import metadata
from pathlib import Path

import gymnasium as gym
import numpy as np
from stable_baselines3 import PPO
from stable_baselines3.common.utils import set_random_seed

from .wrappers import ObservationNoiseWrapper, SafetyPenaltyWrapper


def _version(package_name: str) -> str:
    try:
        return metadata.version(package_name)
    except metadata.PackageNotFoundError:
        return "not-installed"


def _make_environment(config: dict, observation_noise: float) -> gym.Env:
    env = gym.make(config["environment"])
    env = ObservationNoiseWrapper(env, observation_noise)
    env = SafetyPenaltyWrapper(
        env,
        penalty=config["safety_penalty"],
        saturation_threshold=config["actuator_saturation_threshold"],
    )
    return env


def _evaluate(model: PPO, config: dict, observation_noise: float, seed: int) -> dict:
    env = _make_environment(config, observation_noise)
    episode_returns = []
    episode_violations = []
    steps_to_goal = []
    successes = 0
    threshold = config["success_distance_threshold"]

    try:
        for episode_index in range(config["evaluation_episodes"]):
            observation, _ = env.reset(seed=seed + 10000 + episode_index)
            total_return = 0.0
            violations = 0
            first_goal_step = None
            terminated = False
            truncated = False
            step_count = 0

            while not (terminated or truncated):
                action, _ = model.predict(observation, deterministic=True)
                observation, reward, terminated, truncated, info = env.step(action)
                total_return += float(reward)
                step_count += 1
                violations += int(bool(info.get("safety_violation", False)))

                if "reward_dist" not in info:
                    raise RuntimeError(
                        "Reacher did not return its reward_dist diagnostic; refusing to invent a success metric."
                    )
                distance = abs(float(info["reward_dist"]))
                if first_goal_step is None and distance <= threshold:
                    first_goal_step = step_count

            episode_returns.append(total_return)
            episode_violations.append(violations)
            if first_goal_step is not None:
                successes += 1
                steps_to_goal.append(first_goal_step)
    finally:
        env.close()

    episode_count = len(episode_returns)
    return {
        "success_rate": successes / episode_count,
        "mean_episode_return": float(np.mean(episode_returns)),
        "mean_steps_to_goal": float(np.mean(steps_to_goal)) if steps_to_goal else None,
        "safety_violations_per_episode": float(np.mean(episode_violations)),
        "episodes": episode_count,
    }


def run_experiment(config: dict, run_id: str, artifacts_dir: str | Path) -> dict:
    output_dir = Path(artifacts_dir)
    model_dir = output_dir / "models"
    log_dir = output_dir / "logs"
    model_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    model_path = model_dir / run_id
    set_random_seed(config["seed"])

    train_env = _make_environment(config, config["noise_train"])
    started = time.monotonic()
    try:
        model = PPO(
            "MlpPolicy",
            train_env,
            seed=config["seed"],
            n_steps=1000,
            batch_size=100,
            device="cpu",
            verbose=0,
        )
        model.learn(total_timesteps=config["training_steps"], progress_bar=False)
        model.save(str(model_path))
        actual_training_steps = int(model.num_timesteps)
    finally:
        train_env.close()

    clean = _evaluate(model, config, 0.0, config["seed"])
    noisy = _evaluate(model, config, config["noise_eval"], config["seed"])
    elapsed_seconds = time.monotonic() - started

    metrics = {
        "clean_success_rate": clean["success_rate"],
        "noisy_success_rate": noisy["success_rate"],
        "clean_mean_episode_return": clean["mean_episode_return"],
        "noisy_mean_episode_return": noisy["mean_episode_return"],
        "clean_mean_steps_to_goal": clean["mean_steps_to_goal"],
        "noisy_mean_steps_to_goal": noisy["mean_steps_to_goal"],
        "clean_safety_violations_per_episode": clean["safety_violations_per_episode"],
        "noisy_safety_violations_per_episode": noisy["safety_violations_per_episode"],
        "training_steps": actual_training_steps,
        "n_seeds": 1,
        "evaluation_episodes_per_condition": config["evaluation_episodes"],
        "clean": clean,
        "noisy": noisy,
        "training_seconds": round(elapsed_seconds, 3),
    }
    versions = {
        "python": platform.python_version(),
        "gymnasium": _version("gymnasium"),
        "mujoco": _version("mujoco"),
        "stable_baselines3": _version("stable-baselines3"),
        "numpy": _version("numpy"),
    }
    manifest = {
        "model_path": str(model_path.with_suffix(".zip")),
        "algorithm": "PPO",
        "policy": "MlpPolicy (SB3 default 2x64 architecture)",
        "versions": versions,
        "training_seconds": metrics["training_seconds"],
    }
    run_record = {
        "run_id": run_id,
        "config": config,
        "metrics": metrics,
        "artifact_manifest": manifest,
    }
    (log_dir / f"{run_id}.json").write_text(json.dumps(run_record, indent=2) + "\n")
    return {"metrics": metrics, "artifact_manifest": manifest, "versions": versions}
