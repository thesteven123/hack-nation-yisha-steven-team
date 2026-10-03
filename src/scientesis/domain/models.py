from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ExperimentConfig:
    environment: str
    algorithm: str
    noise_train: float
    noise_eval: float
    safety_penalty: float
    seed: int
    training_steps: int
    evaluation_episodes: int
    success_distance_threshold: float
    actuator_saturation_threshold: float
    brief_version: int

    @classmethod
    def from_brief(
        cls,
        brief: dict,
        noise_train: float,
        noise_eval: float,
        safety_penalty: float,
        seed: int,
    ) -> "ExperimentConfig":
        return cls(
            environment=brief["environment"],
            algorithm=brief["algorithm"],
            noise_train=float(noise_train),
            noise_eval=float(noise_eval),
            safety_penalty=float(safety_penalty),
            seed=int(seed),
            training_steps=int(brief["training_steps_per_run"]),
            evaluation_episodes=int(brief["evaluation_episodes"]),
            success_distance_threshold=float(brief["success_distance_threshold"]),
            actuator_saturation_threshold=float(brief["actuator_saturation_threshold"]),
            brief_version=int(brief["version"]),
        )

    def to_dict(self) -> dict:
        return asdict(self)
