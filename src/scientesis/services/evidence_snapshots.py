from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scientesis.services.evidence import load_evidence_content

MAX_INDEXED_SOURCE_CHARS = 16_000


def build_moss_documents(repository, project_id: str, project_root: str | Path) -> list[dict]:
    brief = repository.get_active_brief(project_id)
    documents = [
        _document(
            project_id,
            "research_brief",
            str(brief["version"]),
            _brief_text(brief),
            {"research_brief_version": str(brief["version"])},
        )
    ]

    for version in repository.list_brief_versions(project_id):
        if version["version"] == brief["version"]:
            continue
        documents.append(
            _document(
                project_id,
                "brief-history",
                str(version["version"]),
                f"ResearchBrief change v{version['version']}\nReason: {version['change_reason']}\n{_brief_text(version['brief'])}",
                {"research_brief_version": str(version["version"]), "record_type": "brief_history"},
            )
        )

    for hypothesis in repository.list_hypotheses(project_id, status="approved"):
        if hypothesis["brief_version"] != brief["version"]:
            continue
        documents.append(
            _document(
                project_id,
                "approved_hypothesis",
                hypothesis["id"],
                f"Approved hypothesis\n{hypothesis['original_text']}\n\nOperational protocol\n{_json(hypothesis['protocol'])}",
                {"hypothesis_id": hypothesis["id"], "research_brief_version": str(hypothesis["brief_version"])},
            )
        )

    for run in repository.list_runs(project_id):
        if run["status"] not in {"completed", "failed"}:
            continue
        config = run["config"]
        metrics = run.get("metrics") or {}
        run_text = "\n".join(
            [
                f"Experiment summary {run['id']}",
                f"Status: {run['status']}",
                f"Environment: {config.get('environment')}",
                f"Algorithm: {config.get('algorithm')}",
                f"Brief version: {config.get('brief_version')}",
                f"Condition: training noise {config.get('noise_train')}; evaluation noise {config.get('noise_eval')}; safety-proxy penalty {config.get('safety_penalty')}; seed {config.get('seed')}",
                f"Aggregate metrics: {_json(metrics)}",
                f"Recorded software versions: {_json(run.get('versions') or {})}",
                f"Failure detail: {run.get('error_message') or 'None'}",
            ]
        )
        documents.append(
            _document(
                project_id,
                "failure_report" if run["status"] == "failed" else "experiment_summary",
                run["id"],
                run_text,
                {
                    "experiment_run_id": run["id"],
                    "environment": str(config.get("environment", "")),
                    "algorithm": str(config.get("algorithm", "")),
                    "status": run["status"],
                    "research_brief_version": str(config.get("brief_version", "")),
                    "created_at": str(run.get("completed_at") or run.get("started_at") or ""),
                },
            )
        )

    for report in repository.list_critic_reports(project_id):
        report_text = "\n".join(
            [
                f"Deterministic critic report {report['id']}",
                f"Verdict: {report['verdict']}",
                f"Experiment run IDs: {', '.join(report['experiment_run_ids'] or [])}",
                f"Findings: {_json(report['findings'] or [])}",
                f"Limitations: {_json(report['limitations'] or [])}",
                f"Recommended next action: {_json(report['recommended_next_action'] or {})}",
                "This is a deterministic recommendation, not a scientist's interpretation.",
            ]
        )
        documents.append(
            _document(
                project_id,
                "critic_report",
                report["id"],
                report_text,
                {
                    "critic_report_id": report["id"],
                    "experiment_run_ids_json": _json(report["experiment_run_ids"] or []),
                    "verdict": report["verdict"],
                },
            )
        )

    for decision in repository.list_decisions(project_id):
        alternatives = [
            {"label": option["label"], "rationale": option["rationale"], "risks": option["risks"], "benefits": option["benefits"]}
            for option in decision["options"]
        ]
        answers = [
            {
                "answer_type": answer["answer_type"],
                "selected_option_id": answer["selected_option_id"],
                "custom_response": answer["custom_response"],
                "rationale": answer["rationale_optional"],
                "scope": answer["scope"],
            }
            for answer in decision["answers"]
        ]
        documents.append(
            _document(
                project_id,
                "human_decision",
                decision["id"],
                f"Human decision record {decision['id']}\nQuestion: {decision['question']}\nStatus: {decision['status']}\nResearchBrief version: {decision['pre_brief_version']}\nAlternatives: {_json(alternatives)}\nHuman answers: {_json(answers)}",
                {"decision_id": decision["id"], "status": decision["status"], "research_brief_version": str(decision["pre_brief_version"])},
            )
        )

    for interpretation in repository.list_human_interpretations(project_id):
        documents.append(
            _document(
                project_id,
                "scientist_interpretation",
                interpretation["id"],
                f"Scientist interpretation {interpretation['id']}\nCritic report: {interpretation['critic_report_id']}\nScientist verdict: {interpretation['verdict']}\nRationale: {interpretation['rationale'] or 'None supplied'}",
                {"interpretation_id": interpretation["id"], "critic_report_id": interpretation["critic_report_id"], "verdict": interpretation["verdict"]},
            )
        )

    for card in repository.list_evidence_cards(project_id, approval_status="approved"):
        source_text = load_evidence_content(card, project_root)
        text = "\n\n".join(
            [
                card["title"],
                f"Source: {card['source_url']}",
                f"External claim: {card['claim_text']}",
                f"Scope: {card['scope_text']}",
                f"Limitations: {card['limitations_text']}",
                f"Potential method use: {card['implementation_hint']}",
                source_text[:MAX_INDEXED_SOURCE_CHARS],
            ]
        )
        documents.append(
            _document(
                project_id,
                "external_evidence",
                card["id"],
                text,
                {
                    "evidence_id": card["id"],
                    "source_type": card["source_type"],
                    "source_url": card["source_url"],
                    "content_sha256": card["content_sha256"],
                    "approved_for": "method_design_only",
                    "claim_status": "external_prior_not_local_result",
                },
            )
        )

    return documents


def snapshot_references_for_hits(hits: list[dict], selected_document_ids: list[str], documents: list[dict]) -> dict:
    selected = set(selected_document_ids)
    document_map = {document["id"]: document for document in documents}
    hit_map = {hit["id"]: hit for hit in hits if isinstance(hit.get("id"), str)}
    selected_ids = [document_id for document_id in selected_document_ids if document_id in selected and document_id in document_map and document_id in hit_map]
    evidence_ids = []
    experiment_ids = []
    critic_report_ids = []
    for document_id in selected_ids:
        metadata = document_map[document_id].get("metadata") or hit_map[document_id].get("metadata") or {}
        _append_unique(evidence_ids, metadata.get("evidence_id"))
        _append_unique(experiment_ids, metadata.get("experiment_run_id"))
        _append_unique(critic_report_ids, metadata.get("critic_report_id"))
        report_runs = metadata.get("experiment_run_ids_json")
        if isinstance(report_runs, str):
            try:
                report_runs = json.loads(report_runs)
            except json.JSONDecodeError:
                report_runs = []
        if isinstance(report_runs, list):
            for run_id in report_runs:
                _append_unique(experiment_ids, run_id)
    return {
        "moss_document_ids": selected_ids,
        "source_ids": evidence_ids,
        "experiment_ids": experiment_ids,
        "critic_report_ids": critic_report_ids,
    }


def _document(project_id: str, kind: str, record_id: str, text: str, metadata: dict) -> dict:
    project_hash = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:12]
    record_hash = hashlib.sha256(record_id.encode("utf-8")).hexdigest()[:12]
    normalized_text = str(text).strip()[:MAX_INDEXED_SOURCE_CHARS]
    return {
        "id": f"sc-{project_hash}-{kind}-{record_hash}",
        "text": normalized_text,
        "metadata": {"project_id": project_id, "record_type": kind, **metadata},
    }


def _brief_text(brief: dict) -> str:
    return "\n".join(
        [
            f"Research project: {brief['project_title']}",
            f"Research question: {brief['research_question']}",
            f"Objective: {brief['objective']}",
            f"Primary outcome: {brief['primary_outcome']}",
            f"Permitted environment: {brief['environment']}",
            f"Permitted algorithm: {brief['algorithm']}",
            f"Training seeds: {', '.join(str(seed) for seed in brief['training_seeds'])}",
            f"ResearchBrief version: {brief['version']}",
            "Safety findings concern the recorded simulation proxy only, not physical-robot safety.",
        ]
    )[:MAX_INDEXED_SOURCE_CHARS]


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


def _append_unique(values: list[str], value) -> None:
    if isinstance(value, str) and value and value not in values:
        values.append(value)
