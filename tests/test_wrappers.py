import numpy as np

from scientesis.rl.wrappers import ObservationNoiseWrapper, SafetyPenaltyWrapper


class TinyEnv:
    pass


def test_observation_noise_is_deterministic_for_a_seed():
    import gymnasium as gym

    env_a = ObservationNoiseWrapper(gym.make("Pendulum-v1"), 0.05)
    env_b = ObservationNoiseWrapper(gym.make("Pendulum-v1"), 0.05)
    obs_a, _ = env_a.reset(seed=13)
    obs_b, _ = env_b.reset(seed=13)
    assert np.allclose(obs_a, obs_b)
    env_a.close()
    env_b.close()


def test_safety_wrapper_marks_saturated_action():
    import gymnasium as gym

    env = SafetyPenaltyWrapper(gym.make("Pendulum-v1"), penalty=0.25, saturation_threshold=0.95)
    env.reset(seed=7)
    _, reward, _, _, info = env.step(np.array([1.0], dtype=np.float32))
    assert info["safety_violation"] is True
    assert info["safety_penalty_applied"] == 0.25
    assert reward < 0
    env.close()
