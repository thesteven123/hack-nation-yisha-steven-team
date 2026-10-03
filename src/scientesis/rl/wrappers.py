import numpy as np
import gymnasium as gym


class ObservationNoiseWrapper(gym.Wrapper):
    def __init__(self, env, standard_deviation: float):
        super().__init__(env)
        self.standard_deviation = float(standard_deviation)
        self._rng = np.random.default_rng()

    def _perturb(self, observation):
        observation = np.asarray(observation, dtype=np.float32)
        if self.standard_deviation == 0:
            return observation
        noise = self._rng.normal(0.0, self.standard_deviation, size=observation.shape)
        return (observation + noise).astype(np.float32)

    def reset(self, *, seed=None, options=None):
        if seed is not None:
            self._rng = np.random.default_rng(seed)
        observation, info = self.env.reset(seed=seed, options=options)
        return self._perturb(observation), info

    def step(self, action):
        observation, reward, terminated, truncated, info = self.env.step(action)
        return self._perturb(observation), reward, terminated, truncated, info


class SafetyPenaltyWrapper(gym.Wrapper):
    def __init__(self, env, penalty: float, saturation_threshold: float):
        super().__init__(env)
        self.penalty = float(penalty)
        self.saturation_threshold = float(saturation_threshold)

    def step(self, action):
        observation, reward, terminated, truncated, info = self.env.step(action)
        saturated = bool(np.any(np.abs(np.asarray(action)) >= self.saturation_threshold))
        info = dict(info)
        info["safety_violation"] = saturated
        info["safety_penalty_applied"] = self.penalty if saturated else 0.0
        adjusted_reward = float(reward) - (self.penalty if saturated else 0.0)
        return observation, adjusted_reward, terminated, truncated, info
