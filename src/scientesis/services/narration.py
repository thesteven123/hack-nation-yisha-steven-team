from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from pathlib import Path

from scientesis.adapters.elevenlabs import MAX_AUDIO_BYTES, MAX_TEXT_CHARS, OUTPUT_FORMAT, validate_mp3
from scientesis.db.repository import json_text, new_id, utc_now
from scientesis.services.validation import validate_execution_authorization

CRITIC_VERDICTS = {"supported", "provisional", "inconclusive", "rejected", "invalid_comparison"}
HUMAN_VERDICTS = {"accepted_evidence", "preliminary", "inconclusive", "rejected"}
RECOMMENDATIONS = {
    "replicate": "collect more seed replications",
    "repair_or_rerun": "repair the recorded evidence or request a newly approved rerun",
    "inspect_safety": "investigate the simulation safety proxy",
    "ask_human": "ask the scientist to choose the next direction",
    "review_results": "review the results",
    "propose_intervention": "review a permitted training-noise intervention",
    "replicate_baseline": "collect matched baseline replications",
    "human_interpretation": "record a human interpretation of the result",
    "review_or_refine": "review the tradeoff and choose a bounded next experiment",
}


def build_lab_summary(repository, project_id: str, run_id: str | None = None) -> dict:
    brief = repository.get_active_brief(project_id)
    runs = repository.list_runs(project_id)
    completed = [run for run in runs if run["status"] == "completed"]
    if run_id is None and completed:
        run_id = completed[0]["id"]
    run = next((item for item in completed if item["id"] == run_id), None)
    if run_id is not None and run is None:
        raise ValueError("Narration requires a completed run from this project.")
    state = {"project_id": project_id, "current_brief_version": brief["version"], "run": None, "critic": None, "human_interpretation": None}
    sentences = ["Scientesis captured research summary. This is not live authorization."]
    if run is None:
        sentences.append("There are no completed experiments in this project. No result or research finding can be reported yet.")
    else:
        metrics = _validated_metrics(run["metrics"])
        version = run["config"]["brief_version"]
        run_brief = next((item["brief"] for item in repository.list_brief_versions(project_id) if item["version"] == version), None)
        if run_brief is None:
            raise ValueError("The completed run's ResearchBrief version is missing.")
        condition = {key: value for key, value in run["config"].items() if key != "seed"}
        seeds = sorted({item["seed"] for item in completed if {key: value for key, value in item["config"].items() if key != "seed"} == condition})
        state["run"] = {"id": run["id"], "config": run["config"], "metrics": metrics, "distinct_training_seeds": seeds, "minimum_seeds_for_claim": run_brief["minimum_seeds_for_claim"]}
        sentences.extend([
            f"Experiment {run['id']} completed with training seed {run['seed']} under ResearchBrief version {version}.",
            f"Clean evaluation success was {_number(Decimal(str(metrics['clean_success_rate'])) * 100)} percent. Noisy evaluation success was {_number(Decimal(str(metrics['noisy_success_rate'])) * 100)} percent.",
            f"Clean evaluation recorded {_number(metrics['clean_safety_violations_per_episode'])} actuator saturation events per episode. Noisy evaluation recorded {_number(metrics['noisy_safety_violations_per_episode'])} events per episode.",
            f"These are this run's stored values, not a pooled comparison. This exact condition has {len(seeds)} distinct stored training seeds; its ResearchBrief requires {run_brief['minimum_seeds_for_claim']} for a claim.",
        ])
        if version != brief["version"]:
            sentences.append(f"The current ResearchBrief is version {brief['version']}; this result belongs to the older version {version}.")
        report = next((item for item in repository.list_critic_reports(project_id) if run["id"] in item["experiment_run_ids"]), None)
        if report is None:
            sentences.append("No critic report covers this run. No automated verdict is available.")
        else:
            if report["verdict"] not in CRITIC_VERDICTS:
                raise ValueError("The stored critic verdict is not recognized.")
            action = (report.get("recommended_next_action") or {}).get("action")
            state["critic"] = {"id": report["id"], "verdict": report["verdict"], "experiment_run_ids": report["experiment_run_ids"], "recommended_action": action}
            sentences.append(f"Stored critic report {report['id']} records a {report['verdict']} verdict. This is the automated critic's assessment, not the scientist's interpretation.")
            if action in RECOMMENDATIONS:
                sentences.append(f"The critic recommends that the scientist {RECOMMENDATIONS[action]}. A recommendation is not permission to run another experiment.")
            interpretations = repository.list_human_interpretations(project_id, report["id"])
            if interpretations:
                interpretation = interpretations[0]
                if interpretation["verdict"] not in HUMAN_VERDICTS:
                    raise ValueError("The stored human interpretation is not recognized.")
                state["human_interpretation"] = {"id": interpretation["id"], "critic_report_id": report["id"], "verdict": interpretation["verdict"]}
                sentences.append(f"Separately, scientist interpretation {interpretation['id']} records {interpretation['verdict'].replace('_', ' ')}. This does not authorize new work.")
            else:
                sentences.append("The scientist has not recorded an interpretation of this critic report.")
    approved = _valid_approved_proposal(repository, project_id, brief, runs)
    state["approved_proposal"] = approved
    if approved is None:
        sentences.append("No unstarted proposal currently has valid approval within the active experiment budget.")
    else:
        config = approved["config"]
        sentences.append(f"Proposal {approved['id']} has explicit approval for training noise {_number(config['noise_train'])}, evaluation noise {_number(config['noise_eval'])}, proxy penalty {_number(config['safety_penalty'])}, and seed {config['seed']}, under current ResearchBrief version {brief['version']}.")
        sentences.append("Starting this proposal remains a separate human action and requires a fresh authorization check.")
    sentences.append("Simulation only. Actuator saturation is a proxy, not evidence of physical robot safety or deployment readiness.")
    text = " ".join(sentences)
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError("The grounded narration exceeds the narration length limit.")
    fingerprint = hashlib.sha256(json_text({"state": state, "text": text}).encode("utf-8")).hexdigest()
    return {"captured_at": utc_now(), "fingerprint": fingerprint, "state": state, "text": text}


def create_narration_audio(client, repository, project_id: str, run_id: str, expected_fingerprint: str, project_root: str | Path) -> dict:
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("Choose a completed experiment before generating narration audio.")
    summary = build_lab_summary(repository, project_id, run_id)
    if summary["fingerprint"] != expected_fingerprint:
        raise ValueError("The research state changed. Review the refreshed summary before generating audio.")
    root = Path(project_root).resolve()
    folder = (root / "artifacts" / "audio").resolve()
    if not folder.is_relative_to(root):
        raise ValueError("Narration artifacts must remain inside the project.")
    audio = client.synthesize(summary["text"])
    validate_mp3(audio)
    if build_lab_summary(repository, project_id, run_id)["fingerprint"] != expected_fingerprint:
        raise ValueError("The research state changed while audio was generated. Review a refreshed summary and try again.")
    folder.mkdir(parents=True, exist_ok=True)
    identifier = new_id("NARRATION")
    audio_path = folder / f"{identifier}.mp3"
    text_path = folder / f"{identifier}.md"
    manifest_path = folder / f"{identifier}.json"
    manifest = {
        "id": identifier, "summary": summary, "provider": "elevenlabs", "voice_id": client.settings.voice_id,
        "model_id": client.settings.model_id, "output_format": OUTPUT_FORMAT,
        "audio_path": str(audio_path.relative_to(root)), "text_path": str(text_path.relative_to(root)),
        "manifest_path": str(manifest_path.relative_to(root)),
        "audio_sha256": hashlib.sha256(audio).hexdigest(),
        "text_sha256": hashlib.sha256(summary["text"].encode("utf-8")).hexdigest(),
    }
    created = []
    try:
        for path, content in ((audio_path, audio), (text_path, summary["text"].encode("utf-8")), (manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8"))):
            with path.open("xb") as output:
                created.append(path)
                output.write(content)
        repository.record_narration_artifact(manifest, project_id)
    except Exception as error:
        for path in created:
            path.unlink(missing_ok=True)
        if isinstance(error, OSError):
            raise RuntimeError("Could not save the narration artifacts locally.") from None
        raise
    return manifest


def load_narration_audio(manifest: dict, project_root: str | Path) -> bytes:
    root = Path(project_root).resolve()
    folder = (root / "artifacts" / "audio").resolve()
    path = (root / manifest["audio_path"]).resolve()
    if not folder.is_relative_to(root) or not path.is_relative_to(folder):
        raise ValueError("Narration audio must stay inside the project's audio folder.")
    with path.open("rb") as source:
        audio = source.read(MAX_AUDIO_BYTES + 1)
    validate_mp3(audio)
    if hashlib.sha256(audio).hexdigest() != manifest["audio_sha256"]:
        raise ValueError("Narration audio failed its SHA-256 integrity check.")
    return audio


def _validated_metrics(metrics: dict) -> dict:
    names = ("clean_success_rate", "noisy_success_rate", "clean_safety_violations_per_episode", "noisy_safety_violations_per_episode")
    if not isinstance(metrics, dict):
        raise ValueError("Completed run metrics are missing.")
    result = {}
    for name in names:
        value = metrics.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or "success_rate" in name and value > 1:
            raise ValueError(f"The stored metric {name} is missing or invalid; no narration can be generated.")
        condition, metric = name.split("_", 1)
        block = metrics.get(condition)
        if isinstance(block, dict) and metric in block and block[metric] != value:
            raise ValueError(f"The stored metric {name} disagrees with its condition record.")
        result[name] = value
    return result


def _valid_approved_proposal(repository, project_id: str, brief: dict, runs: list[dict]) -> dict | None:
    if len(runs) >= brief["experiment_budget"]:
        return None
    used = {run["proposal_id"] for run in runs}
    for proposal in repository.list_proposals(project_id):
        if proposal["status"] != "approved" or proposal["id"] in used:
            continue
        try:
            validate_execution_authorization(proposal, brief)
            if proposal.get("hypothesis_id"):
                hypothesis = repository.get_hypothesis(proposal["hypothesis_id"])
                if hypothesis["project_id"] != project_id or hypothesis["status"] != "approved" or hypothesis["brief_version"] != brief["version"]:
                    continue
        except (ValueError, KeyError):
            continue
        return {"id": proposal["id"], "config": proposal["config"], "approval_scope": proposal["approval_scope"]}
    return None


def _number(value) -> str:
    text = format(Decimal(str(value)), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
