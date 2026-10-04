"""Pure, bounded branch context rules awaiting ResearchLabStore integration.

These functions do not grant authority, persist records, reserve quota or run a
worker. The service must call them inside its existing producer -> Lab -> model
transaction order. Every branch belongs to one campaign and shares its ledgers.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

MAX_BRANCHES = 3
MAX_QUESTIONS = 3
HEX = re.compile(r"[0-9a-f]{64}\Z")
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


class BranchError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _require(condition, message, code="invalid_branch"):
    if not condition:
        raise BranchError(code, message)


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _text(value, maximum=4000):
    _require(isinstance(value, str) and value.strip() and len(value.encode()) <= maximum and "\0" not in value, "A bounded nonempty branch field is required")
    return value


def _identity(value):
    _require(isinstance(value, str) and ID.fullmatch(value), "Invalid branch or question identity")
    return value


def _digest(value):
    _require(isinstance(value, str) and HEX.fullmatch(value), "A fixed artifact digest is required")
    return value


def _fields(value, required, optional=()):
    _require(isinstance(value, dict) and set(required) <= set(value) <= set(required) | set(optional), "Unexpected or missing branch fields")


def root_context(campaign):
    """Scientific context, not the brief revision also used by input corrections."""
    brief = campaign["brief"]
    return _hash({"campaign_id": campaign["id"], "adapter": campaign["adapter"],
                  "brief": {key: brief[key] for key in ("goal", "hypothesis", "success_criteria", "constraints", "authorized_actions")},
                  "hypothesis_set_artifact": campaign.get("hypothesis_set_artifact")})


def _branch(state, branch_id, expected_revision=None):
    matches = [b for b in state["branches"] if b["id"] == branch_id]
    _require(len(matches) == 1, "Branch is not present in this campaign", "branch_not_found")
    branch = matches[0]
    if expected_revision is not None:
        _require(type(expected_revision) is int and expected_revision == branch["revision"], "Branch context changed; reload before applying this choice", "branch_revision_conflict")
    return branch


def project_inputs(campaign, inputs, source_ids):
    """Service-held inputs only. Proposals never supply hashes or replacement text."""
    _require(_hash(inputs) == campaign["input_artifact"], "Authoritative current input does not match its frozen digest", "input_integrity")
    if campaign["adapter"]["id"] == "source_evidence":
        _require(isinstance(source_ids, list) and 1 <= len(source_ids) <= 5
                 and all(isinstance(identity, str) and 1 <= len(identity.encode()) <= 128 for identity in source_ids)
                 and len(set(source_ids)) == len(source_ids), "Select one to five distinct supplied source identities")
        sources = {source["id"]: source for source in inputs["sources"]}
        _require(all(identity in sources for identity in source_ids), "A selected source is missing from the current inputs", "branch_input_missing")
        return {**copy.deepcopy(inputs), "sources": [copy.deepcopy(sources[identity]) for identity in source_ids]}
    _require(campaign["adapter"]["id"] == "paired_numeric" and source_ids is None,
             "This branch method cannot reinterpret its parent input schema")
    return copy.deepcopy(inputs)


def _acyclic(branches):
    by_id = {b["id"]: b for b in branches}
    def visit(identity, path):
        _require(identity not in path, "Branch dependencies must be acyclic")
        _require(identity in by_id, "A branch dependency is outside this campaign")
        for edge in by_id[identity]["depends_on"]:
            visit(edge["branch_id"], {*path, identity})
    for identity in by_id:
        visit(identity, set())


def create_set(campaign, proposals, inputs, *, freeze_artifact, at):
    """Explicit conversion, never a getter migration; existing history stays put."""
    _require(isinstance(proposals, list) and 1 <= len(proposals) <= MAX_BRANCHES, "A campaign supports one to three explicit branches")
    _require(not campaign.get("branch_set"), "A branch set already exists; revise it without resetting its lineage")
    _require(callable(freeze_artifact), "The service must persist branch input artifacts")
    context, branches = root_context(campaign), []
    for proposal in proposals:
        _fields(proposal, {"id", "title", "question", "success_criterion", "methods", "depends_on", "questions"}, {"source_ids"})
        methods = proposal["methods"]
        _require(isinstance(methods, list) and methods and all(isinstance(method, str) for method in methods) and len(set(methods)) == len(methods)
                 and set(methods) <= set(campaign["brief"]["authorized_actions"]), "Branches may only restrict already-authorized methods")
        dependencies = proposal["depends_on"]
        _require(isinstance(dependencies, list) and len(dependencies) < MAX_BRANCHES, "Too many branch dependencies")
        for edge in dependencies:
            _fields(edge, {"branch_id", "require"})
            _identity(edge["branch_id"])
            _require(edge["require"] in ("completed", "qc_passed"), "Unknown branch dependency requirement")
        _require(len({e["branch_id"] for e in dependencies}) == len(dependencies), "Duplicate branch dependency")
        questions = proposal["questions"]
        _require(isinstance(questions, list) and len(questions) <= MAX_QUESTIONS, "Too many human questions")
        frozen_questions = []
        for question in questions:
            _fields(question, {"id", "prompt", "required"})
            _identity(question["id"]); _text(question["prompt"])
            _require(type(question["required"]) is bool, "Question requirement must be explicit")
            frozen_questions.append({**copy.deepcopy(question), "prompt_hash": _hash(question), "answer": None})
        _require(len({q["id"] for q in questions}) == len(questions), "Duplicate question identity")
        projected = project_inputs(campaign, inputs, proposal.get("source_ids"))
        digest = freeze_artifact(projected)
        _require(digest == _hash(projected), "Persisted branch input differs from its supplied-source projection", "input_integrity")
        branches.append({"id": _identity(proposal["id"]), "revision": 1, "title": _text(proposal["title"], 200),
                         "question": _text(proposal["question"]), "success_criterion": _text(proposal["success_criterion"]),
                         "methods": copy.deepcopy(methods), "source_ids": copy.deepcopy(proposal.get("source_ids")),
                         "input_artifact": digest, "derived_from_input_artifact": campaign["input_artifact"],
                         "root_context_hash": context, "introduced_brief_revision": campaign["brief"]["revision"],
                         "depends_on": copy.deepcopy(dependencies), "questions": frozen_questions,
                         "control": "active", "owned_work": None, "latest_result": None,
                         "created_at": at, "updated_at": at, "post_outcome": bool(campaign["rounds"]),
                         "scientific_status": "not_validated"})
    _require(len({b["id"] for b in branches}) == len(branches), "Duplicate branch identity")
    _require(sum(len(b["questions"]) for b in branches) <= MAX_QUESTIONS, "Consolidate this checkpoint to at most three questions")
    _acyclic(branches)
    return {"schema_version": 1, "campaign_id": campaign["id"], "authority_epoch": 1, "root_control": "active", "branches": branches,
            "budget_scope": "shared_campaign_and_native_job_ledgers", "automatic_dispatch": False,
            "historical_unassigned_run_ids": [r["run"]["id"] for r in campaign["rounds"] if not r["run"].get("branch_id")]}


def _question_context(branch):
    return _hash({key: branch[key] for key in ("root_context_hash", "question", "success_criterion", "methods", "input_artifact")})


def _answers_current(branch):
    return [q for q in branch["questions"] if q["required"] and not (
        q["answer"] and q["answer"]["prompt_hash"] == q["prompt_hash"]
        and q["answer"]["question_context_hash"] == _question_context(branch))]


def _result_context(branch):
    return _hash({"question_context": _question_context(branch),
                  "answers": [{"question": q["prompt_hash"], "answer": (q["answer"] or {}).get("artifact")} for q in branch["questions"]]})


def _dependency_bindings(campaign, state, branch, inputs, dependency_state):
    bindings, blockers = [], []
    for edge in branch["depends_on"]:
        upstream = _branch(state, edge["branch_id"])
        result = upstream["latest_result"]
        upstream_allowed = gate(campaign, state, upstream["id"], inputs, dependency_state, publication=True)["allowed"]
        if (not upstream_allowed or upstream["control"] != "active" or upstream["owned_work"] is not None or _answers_current(upstream) or not result
                or result["input_artifact"] != upstream["input_artifact"] or result["root_context_hash"] != upstream["root_context_hash"]
                or result["context_hash"] != _result_context(upstream)
                or result["status"] != "completed" or edge["require"] == "qc_passed" and not result["qc_passed"]):
            blockers.append({"code": "dependency_pending", "branch_id": upstream["id"]})
        else:
            bindings.append({"branch_id": upstream["id"], "branch_revision": upstream["revision"], "require": edge["require"],
                             "run_id": result["run_id"], "round_artifact": result["round_artifact"],
                             "observation_artifact": result["observation_artifact"], "input_artifact": result["input_artifact"]})
    return bindings, blockers


def gate(campaign, state, branch_id, inputs, dependency_state, *, publication=False):
    """dependency_state is computed by the service while holding producer lock."""
    _require(state["campaign_id"] == campaign["id"], "Branch set belongs to another campaign")
    _require(1 <= len(state["branches"]) <= MAX_BRANCHES and len({b["id"] for b in state["branches"]}) == len(state["branches"]), "Malformed persisted branch set")
    _acyclic(state["branches"])
    branch = _branch(state, branch_id)
    blockers = []
    if state["root_control"] != "active" or campaign["status"] == "stopped":
        blockers.append({"code": "root_" + state["root_control"] if state["root_control"] != "active" else "root_stopped"})
    if branch["control"] != "active":
        blockers.append({"code": "branch_" + branch["control"]})
    if branch["root_context_hash"] != root_context(campaign):
        blockers.append({"code": "root_context_changed"})
    try:
        projected = project_inputs(campaign, inputs, branch["source_ids"])
        if _hash(projected) != branch["input_artifact"]:
            blockers.append({"code": "branch_input_changed"})
    except BranchError as exc:
        blockers.append({"code": exc.code})
    if branch["input_artifact"] not in dependency_state.get("registered_input_artifacts", []):
        blockers.append({"code": "dependency_unregistered"})
    if branch["input_artifact"] in dependency_state.get("blocked_input_artifacts", []):
        blockers.append({"code": "dependency_stale"})
    blockers.extend({"code": "required_answer", "question_id": q["id"]} for q in _answers_current(branch))
    bindings, pending = _dependency_bindings(campaign, state, branch, inputs, dependency_state)
    blockers.extend(pending)
    if branch["owned_work"] and not publication:
        blockers.append({"code": "branch_busy"})
    return {"allowed": not blockers, "blockers": blockers, "dependency_refs": bindings}


def freeze_scope(campaign, state, branch_id, inputs, dependency_state, *, publication=False):
    allowed = gate(campaign, state, branch_id, inputs, dependency_state, publication=publication)
    _require(allowed["allowed"], "Branch work is blocked by its relevant context or dependency", "branch_blocked")
    branch = _branch(state, branch_id)
    value = {"campaign_id": campaign["id"], "branch_id": branch_id, "branch_revision": branch["revision"],
             "root_context_hash": root_context(campaign), "authority_epoch": state["authority_epoch"],
             "input_artifact": branch["input_artifact"], "dependency_refs": allowed["dependency_refs"],
             "question_hash": _hash({key: branch[key] for key in ("question", "success_criterion", "methods", "questions")})}
    return {**value, "sha256": _hash(value)}


def check_scope(campaign, state, frozen, inputs, dependency_state):
    expected = freeze_scope(campaign, state, frozen["branch_id"], inputs, dependency_state, publication=True)
    _require(expected == frozen, "Relevant branch context changed; retain diagnostics without publishing this result", "branch_scope_changed")
    return expected


def check_shared_budget(campaign, *, native_quota=None):
    """Admission check only: caller must reserve in the same authoritative txn."""
    if native_quota is not None:
        _require(native_quota["used"] + native_quota["reserved"] < native_quota["limit_jobs"], "Shared campaign native-job quota is exhausted", "budget_exhausted")
        return
    budget = campaign["budget"]
    _require(budget["used_actions"] + budget["reserved_actions"] < budget["max_actions"]
             and len(campaign["rounds"]) + budget["reserved_actions"] < budget["max_rounds"],
             "Shared campaign local action or round quota is exhausted", "budget_exhausted")


def answer_question(campaign, state, branch_id, expected_revision, question_id, answer, *, at, freeze_artifact):
    changed = copy.deepcopy(state)
    branch = _branch(changed, branch_id, expected_revision)
    _require(branch["root_context_hash"] == root_context(campaign), "The root context changed before this answer", "branch_scope_changed")
    question = next((q for q in branch["questions"] if q["id"] == question_id), None)
    _require(question is not None, "Question is not in the current branch", "question_not_found")
    record = {"campaign_id": campaign["id"], "branch_id": branch_id, "branch_revision": expected_revision,
              "question_id": question_id, "prompt_hash": question["prompt_hash"], "prompt": question["prompt"],
              "root_context_hash": branch["root_context_hash"], "question_context_hash": _question_context(branch),
              "answer": _text(answer, 8000), "at": at,
              "actor": "native_user", "scientific_status": "human_statement_not_validated"}
    digest = freeze_artifact(record)
    _require(digest == _hash(record), "Human answer artifact differs", "input_integrity")
    question["answer"] = {**record, "artifact": digest}
    branch.update(revision=branch["revision"] + 1, updated_at=at)
    return changed, record


def rebind_branch(campaign, state, branch_id, expected_revision, inputs, *, at, freeze_artifact, changes=None):
    """Explicit new planning context; does not reinterpret or erase old results."""
    changed = copy.deepcopy(state)
    branch = _branch(changed, branch_id, expected_revision)
    _require(branch["owned_work"] is None and branch["control"] != "stopped", "Settle owned work or start a new explicit branch before replanning", "branch_busy")
    changes = changes or {}
    _fields(changes, (), {"title", "question", "success_criterion", "methods", "source_ids"})
    for key in ("title", "question", "success_criterion"):
        if key in changes:
            branch[key] = _text(changes[key], 200 if key == "title" else 4000)
    methods = changes.get("methods", branch["methods"])
    _require(isinstance(methods, list) and methods and all(isinstance(method, str) for method in methods) and len(set(methods)) == len(methods)
             and set(methods) <= set(campaign["brief"]["authorized_actions"]), "A revision cannot grant new method permissions")
    source_ids = changes.get("source_ids", branch["source_ids"])
    projected = project_inputs(campaign, inputs, source_ids)
    digest = freeze_artifact(projected)
    _require(digest == _hash(projected), "Revised branch input was not frozen correctly", "input_integrity")
    branch.update(methods=copy.deepcopy(methods), source_ids=copy.deepcopy(source_ids), input_artifact=digest,
                  derived_from_input_artifact=campaign["input_artifact"], root_context_hash=root_context(campaign),
                  revision=branch["revision"] + 1, updated_at=at,
                  post_outcome=branch["post_outcome"] or bool(campaign["rounds"]))
    return changed


def control_branch(state, branch_id, expected_revision, operation, *, at):
    _require(operation in {"pause", "resume", "stop"}, "Unsupported branch control")
    changed = copy.deepcopy(state)
    branch = _branch(changed, branch_id, expected_revision)
    _require(not (branch["control"] == "stopped" and operation != "stop"), "A stopped branch requires a new explicit research plan")
    branch.update(control={"pause": "paused", "resume": "active", "stop": "stopped"}[operation], revision=branch["revision"] + 1, updated_at=at)
    return changed


def control_root(state, operation):
    _require(operation in {"pause", "resume", "stop"}, "Unsupported campaign control")
    _require(not (state["root_control"] == "stopped" and operation != "stop"), "A stopped campaign cannot resume without an explicit new plan")
    changed = copy.deepcopy(state)
    changed.update(root_control={"pause": "paused", "resume": "active", "stop": "stopped"}[operation], authority_epoch=state["authority_epoch"] + 1)
    return changed


def mark_owned_work(state, branch_id, work_id, kind, frozen_scope):
    _require(kind in {"local_action", "native_role"}, "Unsupported branch executor")
    changed = copy.deepcopy(state)
    branch = _branch(changed, branch_id)
    _require(branch["owned_work"] is None, "This branch already owns work", "branch_busy")
    _require(frozen_scope["branch_id"] == branch_id and frozen_scope["campaign_id"] == state["campaign_id"]
             and frozen_scope["branch_revision"] == branch["revision"] and frozen_scope["authority_epoch"] == state["authority_epoch"]
             and frozen_scope["root_context_hash"] == branch["root_context_hash"] and frozen_scope["input_artifact"] == branch["input_artifact"]
             and state["root_control"] == "active" and branch["control"] == "active", "Owned work does not match its branch scope")
    branch["owned_work"] = {"id": _text(work_id, 128), "kind": kind, "scope": copy.deepcopy(frozen_scope),
                            "cancellation_requested": False, "outcome": "running"}
    return changed


def request_cancel(state, branch_id, work_id, *, at):
    changed = copy.deepcopy(state)
    branch = _branch(changed, branch_id)
    owned = branch["owned_work"]
    _require(owned is not None and owned["id"] == work_id, "Only this branch's owned work can be cancelled", "work_not_owned")
    owned.update(cancellation_requested=True, outcome="cancellation_pending")
    branch.update(control="paused", revision=branch["revision"] + 1, updated_at=at)
    return changed


def acknowledge_work(state, branch_id, work_id, outcome, *, cleanup_acknowledged):
    _require(outcome in {"completed", "failed", "cancelled", "unknown_outcome"}, "Unknown execution outcome")
    changed = copy.deepcopy(state)
    branch = _branch(changed, branch_id)
    owned = branch["owned_work"]
    _require(owned is not None and owned["id"] == work_id, "A different attempt owns this branch", "work_not_owned")
    known = outcome != "unknown_outcome" and cleanup_acknowledged is True
    if known:
        branch["owned_work"] = None
    else:
        owned["outcome"] = "unknown_outcome" if outcome == "unknown_outcome" else "cleanup_pending"
    return changed, {"acknowledged": known, "may_release_unused_reservation": known,
                     "usage_may_be_unknown": True, "model_used_slots_are_not_refunded": True}


def accept_local_result(campaign, state, frozen, inputs, dependency_state, result_ref, *, at):
    """Called with a reference constructed from an actual committed run, not UI."""
    check_scope(campaign, state, frozen, inputs, dependency_state)
    _fields(result_ref, {"run_id", "round_artifact", "observation_artifact", "input_artifact", "status", "qc_passed"})
    for key in ("round_artifact", "observation_artifact", "input_artifact"):
        _digest(result_ref[key])
    _require(result_ref["input_artifact"] == frozen["input_artifact"] and result_ref["status"] in {"completed", "failed"}
             and type(result_ref["qc_passed"]) is bool, "Actual result does not match the frozen branch input")
    changed = copy.deepcopy(state)
    branch = _branch(changed, frozen["branch_id"])
    _require(branch["owned_work"] is not None and branch["owned_work"]["kind"] == "local_action"
             and branch["owned_work"]["id"] == result_ref["run_id"], "Only the authoritative local attempt may publish this branch result", "work_not_owned")
    branch.update(latest_result={**copy.deepcopy(result_ref), "root_context_hash": branch["root_context_hash"], "context_hash": _result_context(branch),
                                 "dependency_refs": copy.deepcopy(frozen["dependency_refs"])},
                  revision=branch["revision"] + 1, updated_at=at)
    return changed


def result_current(campaign, state, branch_id, inputs, dependency_state):
    """Scientific-context currency is separate from new-execution permission."""
    _acyclic(state["branches"])
    branch = _branch(state, branch_id)
    result = branch.get("latest_result")
    if (not result or branch["root_context_hash"] != root_context(campaign)
            or result["root_context_hash"] != branch["root_context_hash"]
            or result["context_hash"] != _result_context(branch)
            or result["input_artifact"] != branch["input_artifact"]
            or branch["input_artifact"] not in dependency_state.get("registered_input_artifacts", [])
            or branch["input_artifact"] in dependency_state.get("blocked_input_artifacts", [])):
        return False
    try:
        if _hash(project_inputs(campaign, inputs, branch["source_ids"])) != branch["input_artifact"]:
            return False
    except BranchError:
        return False
    current = []
    for edge in branch["depends_on"]:
        if not result_current(campaign, state, edge["branch_id"], inputs, dependency_state):
            return False
        upstream = _branch(state, edge["branch_id"])["latest_result"]
        if upstream["status"] != "completed" or edge["require"] == "qc_passed" and not upstream["qc_passed"]:
            return False
        current.append({"branch_id": edge["branch_id"], "require": edge["require"],
                        **{key: upstream[key] for key in ("run_id", "round_artifact", "observation_artifact", "input_artifact")}})
    prior = [{k: v for k, v in ref.items() if k != "branch_revision"} for ref in result.get("dependency_refs", [])]
    return prior == current


def verify_persisted(campaign, artifact):
    """Offline structure/reference checks, never currentness or scientific proof."""
    state = campaign.get("branch_set")
    if state is None:
        return
    _require(state["campaign_id"] == campaign["id"] and state["schema_version"] == 1
             and type(state["authority_epoch"]) is int and state["authority_epoch"] > 0
             and state["root_control"] in {"active", "paused", "stopped"}, "Invalid branch root authority")
    branches = state["branches"]
    _require(1 <= len(branches) <= MAX_BRANCHES and len({b["id"] for b in branches}) == len(branches), "Invalid branch identities")
    _acyclic(branches)
    rounds = {r["run"]["id"]: r for r in campaign["rounds"]}
    for branch in branches:
        _identity(branch["id"])
        _require("budget" not in branch and type(branch["revision"]) is int and branch["revision"] > 0
                 and branch["methods"] and set(branch["methods"]) <= set(campaign["brief"]["authorized_actions"]),
                 "A saved branch cannot add authority or quota")
        parent = artifact(branch["derived_from_input_artifact"])
        frozen = artifact(branch["input_artifact"])
        _require(_hash(project_inputs({**campaign, "input_artifact": branch["derived_from_input_artifact"]}, parent, branch["source_ids"]))
                 == _hash(frozen) == branch["input_artifact"], "Branch input projection differs from its frozen parent")
        for question in branch["questions"]:
            _require(question["prompt_hash"] == _hash({key: question[key] for key in ("id", "prompt", "required")}), "Saved question identity changed")
            if question["answer"]:
                answer = question["answer"]
                _require(artifact(answer["artifact"]) == {k: v for k, v in answer.items() if k != "artifact"}, "Saved exact answer differs from its artifact")
        latest = branch.get("latest_result")
        if latest:
            record = rounds.get(latest["run_id"])
            _require(record is not None and record["run"].get("branch_id") == branch["id"]
                     and record["run"]["observation_artifact"] == latest["observation_artifact"]
                     and record["run"]["input_artifact"] == latest["input_artifact"]
                     and record["run"]["status"] == latest["status"] and record["qc"]["passed"] == latest["qc_passed"],
                     "Branch latest result differs from the single campaign run ledger")
            _require(artifact(latest["round_artifact"]) == {k: v for k, v in record.items() if k != "artifact"}, "Branch round artifact differs from its run record")
