"""Same-campaign branch operations; called only through service-owned guards."""
from __future__ import annotations

import copy
from contextlib import contextmanager

import research_branches as rules


class BranchController:
    def replay_branch_mutation(self, campaign_id, operation, request, branch_id=None):
        """Read-only replay before producer/dependency admission, including enable."""
        from research_lab import LabError, _fields, _integer
        if operation == "branches_enabled":
            if branch_id is not None:
                raise LabError("invalid_request", "Branch enable targets the campaign")
            _fields(request, {"expected_revision", "idempotency_key", "branches"})
            _integer(request["expected_revision"], 1, 2147483647)
            identity = {"operation": operation, **request}
        else:
            shapes = {"branch_planned": (set(), {"changes"}), "branch_answered": ({"answers"}, set()),
                      "branch_selected": ({"selected_action_id", "feedback"}, set()),
                      "branch_controlled": ({"operation", "feedback"}, set()), "branch_action_executed": (set(), set())}
            if operation not in shapes or not isinstance(branch_id, str):
                raise LabError("invalid_request", "Unknown scoped branch mutation")
            required, optional = shapes[operation]
            _fields(request, {"expected_branch_revision", "expected_authority_epoch", "idempotency_key", *required}, optional)
            _integer(request["expected_branch_revision"], 1, 2147483647)
            _integer(request["expected_authority_epoch"], 1, 2147483647)
            identity = {"operation": operation, "branch_id": branch_id, **request}
        with self._db() as db:
            self._load(db, campaign_id)
            replay, _ = self._replay(db, campaign_id, request["idempotency_key"], identity)
            return self.project(replay) if replay is not None else None

    @staticmethod
    def _branch_error(error):
        from research_lab import LabError
        raise LabError(error.code, error.message) from None

    def _branch_inputs(self, db, item):
        return {branch["id"]: self._read_artifact(db, branch["input_artifact"])
                for branch in item["branch_set"]["branches"]}

    def _branch_dependencies(self, db, item, callback):
        if callback is None:
            return {"known": False, "registered_input_artifacts": [], "blocked_input_artifacts": []}
        return callback(item, self._branch_inputs(db, item))

    def _branch_projection(self, db, item, dependency_state=None):
        result = copy.deepcopy(item)
        state = result.get("branch_set")
        if not state:
            return self.project(result)
        inputs = self._read_artifact(db, item["input_artifact"])
        dependencies = self._branch_dependencies(db, item, dependency_state)
        for branch in state["branches"]:
            branch["gate"] = rules.gate(item, state, branch["id"], inputs, dependencies)
            branch["current_scope"] = None
            branch["scope_status"] = "unknown" if not dependencies.get("known", True) else "blocked"
            if branch["gate"]["allowed"]:
                branch["current_scope"] = rules.freeze_scope(item, state, branch["id"], inputs, dependencies)
                branch["scope_status"] = "current"
            pending = {q["id"] for q in rules._answers_current(branch)}
            for question in branch["questions"]:
                question["needs_answer"] = question["id"] in pending
            latest = branch.get("latest_result")
            if latest:
                latest["current"] = rules.result_current(item, state, branch["id"], inputs, dependencies)
        return self.project(result)

    def branch_snapshot(self, campaign_id, *, dependency_state=None):
        with self._db() as db:
            return self._branch_projection(db, self._load(db, campaign_id), dependency_state)

    def _branch_virtual(self, item, branch):
        value = copy.deepcopy(item)
        value.pop("branch_set", None)
        value["_global_round_count"] = len(item["rounds"])
        value["rounds"] = [r for r in value["rounds"] if r["run"].get("branch_id") == branch["id"]]
        run_ids = {r["run"]["id"] for r in value["rounds"]}
        value["claims"] = [c for c in value["claims"] if c["run_id"] in run_ids]
        value["comparisons"] = [c for c in value.get("comparisons", []) if all(r in run_ids for r in c["run_ids"])]
        value["brief"].update(goal=branch["question"], success_criteria=branch["success_criterion"],
            authorized_actions=branch["methods"], revision=branch.get("plan_context_revision", branch["revision"]))
        value.update(input_artifact=branch["input_artifact"], input_summary=branch["input_summary"],
                     current_plan=copy.deepcopy(branch.get("current_plan")), review=copy.deepcopy(branch.get("review")),
                     status=branch["status"], stop_reason=branch.get("stop_reason"),
                     decisions=copy.deepcopy(branch.get("decisions", [])), total_decisions=branch.get("total_decisions", 0))
        return value

    def _freeze_branch_plan(self, db, item, branch, dependency_state):
        from research_lab import LabError, _now
        deps = self._branch_dependencies(db, item, dependency_state)
        scope = rules.freeze_scope(item, item["branch_set"], branch["id"], self._read_artifact(db, item["input_artifact"]), deps)
        branch["plan_context_revision"] = branch["revision"]
        view = self._branch_virtual(item, branch)
        self._plan(db, view, (branch.get("review") or {}).get("next_method"))
        plan = view["current_plan"]
        if plan:
            plan["candidates"] = [spec for spec in plan["candidates"] if spec["method"] in branch["methods"]]
            if not plan["candidates"]:
                plan = None
                view.update(status="needs_input", stop_reason="needs_method")
            else:
                plan["selected_action_id"] = plan["candidates"][0]["id"]
                plan.update(branch_id=branch["id"], branch_scope=scope)
                for summary in plan["candidates"]:
                    spec = self._read_artifact(db, summary["spec_artifact"])
                    spec.update(branch_id=branch["id"], branch_scope=scope)
                    if branch["post_outcome"]:
                        spec["research_mode"] = summary["research_mode"] = "post_outcome_exploratory"
                    packet = spec["task_packet"]
                    packet.update(branch_id=branch["id"], branch_scope=scope, campaign_goal={
                        "objective": item["brief"]["goal"], "success_criterion": item["brief"]["success_criteria"]})
                    packet["relevant_state"]["branch_answers"] = copy.deepcopy(branch["questions"])
                    packet["relevant_state"]["branch_dependencies"] = scope["dependency_refs"]
                    packet["input_refs"] += [q["answer"]["artifact"] for q in branch["questions"] if q["answer"]]
                    packet["input_refs"] += [ref["observation_artifact"] for ref in scope["dependency_refs"]]
                    digest = self._artifact(db, item["id"], spec)
                    summary.update(branch_id=branch["id"], branch_scope=scope, spec_artifact=digest)
        branch.update(current_plan=plan, status=view["status"], stop_reason=view.get("stop_reason"), updated_at=_now())

    def enable_branches(self, campaign_id, request, *, dependency_state=None):
        from research_lab import _now
        def apply(db, item):
            try:
                item["branch_set"] = rules.create_set(item, request["branches"], self._read_artifact(db, item["input_artifact"]),
                    freeze_artifact=lambda value: self._artifact(db, item["id"], value), at=_now())
                for branch in item["branch_set"]["branches"]:
                    branch.update(status="needs_input", current_plan=None, decisions=[], total_decisions=0, review=None,
                                  input_summary=self._adapter(item).summary(self._read_artifact(db, branch["input_artifact"])))
                dependencies = self._branch_dependencies(db, item, dependency_state)
                for branch in item["branch_set"]["branches"]:
                    if rules.gate(item, item["branch_set"], branch["id"], self._read_artifact(db, item["input_artifact"]), dependencies)["allowed"]:
                        self._freeze_branch_plan(db, item, branch, dependency_state)
                item.update(current_plan=None, status="planned", stop_reason=None)
                return {"branch_ids": [b["id"] for b in item["branch_set"]["branches"]], "shared_budget": True}
            except rules.BranchError as exc:
                self._branch_error(exc)
        return self._mutate(campaign_id, request, "branches_enabled", apply, {"branches"})

    def _mutate_branch(self, campaign_id, branch_id, request, operation, callback, required=(), optional=(), dependency_state=None):
        from research_lab import LabError, _fields, _integer
        _fields(request, {"expected_branch_revision", "expected_authority_epoch", "idempotency_key", *required}, optional)
        _integer(request["expected_branch_revision"], 1, 2147483647)
        _integer(request["expected_authority_epoch"], 1, 2147483647)
        with self._db(write=True) as db:
            item = self._load(db, campaign_id)
            replay, fingerprint = self._replay(db, campaign_id, request["idempotency_key"], {"operation": operation, "branch_id": branch_id, **request})
            if replay is not None:
                return self.project(replay)
            try:
                if not item.get("branch_set"):
                    raise LabError("branch_not_found", "Explicitly enable branches before using this action")
                branch = rules._branch(item["branch_set"], branch_id, request["expected_branch_revision"])
                if item["branch_set"]["authority_epoch"] != request["expected_authority_epoch"]:
                    raise LabError("branch_scope_changed", "Root authorization changed; reload before this branch action")
                item["revision"] += 1
                data = callback(db, item, branch)
                self._save(db, item, operation, {"branch_id": branch_id, **data})
                return self._remember(db, campaign_id, request["idempotency_key"], fingerprint, item)
            except rules.BranchError as exc:
                self._branch_error(exc)

    def plan_branch(self, campaign_id, branch_id, request, *, dependency_state=None):
        from research_lab import _now
        def apply(db, item, branch):
            item["branch_set"] = rules.rebind_branch(item, item["branch_set"], branch_id, branch["revision"],
                self._read_artifact(db, item["input_artifact"]), at=_now(), changes=request.get("changes"),
                freeze_artifact=lambda value: self._artifact(db, item["id"], value))
            branch = rules._branch(item["branch_set"], branch_id)
            branch["input_summary"] = self._adapter(item).summary(self._read_artifact(db, branch["input_artifact"]))
            self._freeze_branch_plan(db, item, branch, dependency_state)
            return {"plan_id": (branch["current_plan"] or {}).get("id"), "branch_revision": branch["revision"]}
        return self._mutate_branch(campaign_id, branch_id, request, "branch_planned", apply, optional={"changes"}, dependency_state=dependency_state)

    def answer_branch(self, campaign_id, branch_id, request, *, dependency_state=None):
        from research_lab import LabError, _fields, _now
        def apply(db, item, branch):
            answers = request["answers"]
            if not isinstance(answers, list) or not 1 <= len(answers) <= 3:
                raise LabError("invalid_request", "Answer one to three current questions atomically")
            ids = set(); start_revision = branch["revision"]
            for answer in answers:
                _fields(answer, {"question_id", "answer"})
                if answer["question_id"] in ids:
                    raise LabError("invalid_request", "A batch cannot answer one question twice")
                ids.add(answer["question_id"])
                item["branch_set"], _ = rules.answer_question(item, item["branch_set"], branch_id,
                    rules._branch(item["branch_set"], branch_id)["revision"], answer["question_id"], answer["answer"], at=_now(),
                    freeze_artifact=lambda value: self._artifact(db, item["id"], value))
            branch = rules._branch(item["branch_set"], branch_id)
            branch.update(current_plan=None, status="needs_input")
            return {"question_ids": sorted(ids), "prior_branch_revision": start_revision, "branch_revision": branch["revision"], "execution_performed": False}
        return self._mutate_branch(campaign_id, branch_id, request, "branch_answered", apply, {"answers"}, dependency_state=dependency_state)

    def decide_branch(self, campaign_id, branch_id, request, *, dependency_state=None):
        from research_lab import LabError, _id, _now, _text
        def apply(db, item, branch):
            plan = branch.get("current_plan")
            scope = rules.freeze_scope(item, item["branch_set"], branch_id, self._read_artifact(db, item["input_artifact"]),
                                       self._branch_dependencies(db, item, dependency_state))
            if plan and self._scientific_scope(plan["branch_scope"]) != self._scientific_scope(scope):
                raise LabError("branch_scope_changed", "Prepare a current plan before selecting its candidate")
            if not plan or branch["status"] != "planned" or request["selected_action_id"] not in {s["id"] for s in plan["candidates"]}:
                raise LabError("invalid_request", "Choose an action from this branch's current plan")
            record = {"id": _id("decision"), "kind": "select", "feedback": _text(request["feedback"], empty=True), "at": _now(),
                      "actor": "native_user", "branch_id": branch_id, "branch_revision": branch["revision"],
                      "brief_revision": branch["plan_context_revision"], "campaign_revision": item["revision"] - 1,
                      "plan_id": plan["id"], "selected_action_id": request["selected_action_id"], "input_artifact": branch["input_artifact"],
                      "scope": "Branch preference within frozen local permissions; not scientific validation"}
            branch["decisions"].append(record); branch["total_decisions"] += 1
            branch["decisions"] = branch["decisions"][-3:]
            plan.update(selected_action_id=record["selected_action_id"], selection_origin="human", selection_reason=record["feedback"] or "Explicit human selection",
                        decision_id=record["id"], decision_revision=item["revision"], decision_artifact=self._artifact(db, item["id"], record))
            branch["revision"] += 1
            return record
        return self._mutate_branch(campaign_id, branch_id, request, "branch_selected", apply, {"selected_action_id", "feedback"}, dependency_state=dependency_state)

    def control_branch(self, campaign_id, branch_id, request, *, dependency_state=None):
        from research_lab import _text, _now
        def apply(db, item, branch):
            item["branch_set"] = rules.control_branch(item["branch_set"], branch_id, branch["revision"], request["operation"], at=_now())
            changed = rules._branch(item["branch_set"], branch_id)
            changed["status"] = {"pause": "paused", "stop": "stopped", "resume": "planned" if changed.get("current_plan") else "needs_input"}[request["operation"]]
            return {"operation": request["operation"], "feedback": _text(request["feedback"], empty=True), "cleanup_acknowledged": False,
                    "scope": "Changes branch publication/execution eligibility; owned native cancellation uses the explicit job cancel action"}
        return self._mutate_branch(campaign_id, branch_id, request, "branch_controlled", apply, {"operation", "feedback"}, dependency_state=dependency_state)

    @staticmethod
    def _scientific_scope(scope):
        return {key: value for key, value in scope.items() if key not in {"sha256", "branch_revision"}}

    def run_branch(self, campaign_id, branch_id, request, *, dependency_state=None):
        from research_lab import LabError, _now
        def apply(db, item, branch):
            dependencies = self._branch_dependencies(db, item, dependency_state)
            inputs = self._read_artifact(db, item["input_artifact"])
            scope = rules.freeze_scope(item, item["branch_set"], branch_id, inputs, dependencies)
            plan = branch.get("current_plan")
            if not plan or self._scientific_scope(plan["branch_scope"]) != self._scientific_scope(scope):
                raise LabError("branch_scope_changed", "Freeze a current branch plan before executing")
            rules.check_shared_budget(item)
            view = self._branch_virtual(item, branch)
            view["_branch_dispatch_scope"] = scope
            prior_rounds, prior_comparisons = len(view["rounds"]), len(view["comparisons"])
            result = self._execute_action(db, view, request)
            record = view["rounds"][-1]
            record["index"] = len(item["rounds"]) + 1
            record["run"].update(branch_id=branch_id, branch_scope=scope)
            record["artifact"] = self._artifact(db, item["id"], record)
            item["rounds"].append(record)
            item["budget"] = view["budget"]
            claim = view["claims"][-1]; claim["branch_id"] = branch_id
            item["claims"].append(claim)
            item["comparisons"].extend(view["comparisons"][prior_comparisons:])
            item["branch_set"] = rules.mark_owned_work(item["branch_set"], branch_id, record["run"]["id"], "local_action", scope)
            item["branch_set"] = rules.accept_local_result(item, item["branch_set"], scope, inputs, dependencies, {
                "run_id": record["run"]["id"], "round_artifact": record["artifact"], "observation_artifact": record["run"]["observation_artifact"],
                "input_artifact": branch["input_artifact"], "status": record["run"]["status"], "qc_passed": record["qc"]["passed"]}, at=_now())
            item["branch_set"], _ = rules.acknowledge_work(item["branch_set"], branch_id, record["run"]["id"], record["run"]["status"], cleanup_acknowledged=True)
            changed = rules._branch(item["branch_set"], branch_id)
            changed.update(review=view["review"], status=view["status"], stop_reason=view["stop_reason"])
            return result
        return self._mutate_branch(campaign_id, branch_id, request, "branch_action_executed", apply, dependency_state=dependency_state)

    def _branch_model_view(self, db, item, branch_id, dependency_state):
        branch = rules._branch(item["branch_set"], branch_id)
        scope = rules.freeze_scope(item, item["branch_set"], branch_id, self._read_artifact(db, item["input_artifact"]),
                                   self._branch_dependencies(db, item, dependency_state), publication=True)
        view = self._branch_virtual(item, branch)
        view.update(branch_id=branch_id, branch_scope=scope, campaign_goal=copy.deepcopy(item["brief"]),
                    branch_questions=copy.deepcopy(branch["questions"]), total_rounds=len(view["rounds"]))
        return view

    @contextmanager
    def branch_scope_guard(self, campaign_id, branch_id, *, dependency_state):
        """Caller first holds producer lock; keep this lock through model commit."""
        with self._db(write=True) as db:
            try:
                yield self._branch_model_view(db, self._load(db, campaign_id), branch_id, dependency_state)
            except rules.BranchError as exc:
                self._branch_error(exc)

    def branch_model_snapshot(self, campaign_id, branch_id, *, dependency_state):
        with self._db() as db:
            try:
                return self._branch_model_view(db, self._load(db, campaign_id), branch_id, dependency_state)
            except rules.BranchError as exc:
                self._branch_error(exc)
