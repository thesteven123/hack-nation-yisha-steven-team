from __future__ import annotations

import streamlit as st

from scientesis.services.critic import critique_run
from scientesis.services.planner import plan_next_work


def render_planner_tab(repository, project_id: str) -> None:
    st.subheader("Deterministic next-work planner")
    st.caption(
        "The planner applies visible rules to the ResearchBrief and recorded runs. It does not call an LLM, "
        "change the ResearchBrief, approve hypotheses or proposals, or start experiments."
    )

    if st.button("Assess the next authorized work", key="planner_assess"):
        try:
            st.session_state["scientesis_planner_result"] = plan_next_work(repository, project_id)
        except (ValueError, KeyError) as error:
            st.error(str(error))

    plan = st.session_state.get("scientesis_planner_result")
    if not isinstance(plan, dict):
        st.info("Run the assessment to see a bounded recommendation.")
        return

    st.markdown(f"### {plan['status'].replace('_', ' ').title()}")
    st.write(plan["reason"])
    context = plan.get("context", {})
    left, middle, right = st.columns(3)
    left.metric("Completed runs", context.get("completed_runs", 0))
    middle.metric("Runs remaining in brief", context.get("budget_remaining", 0))
    right.metric("ResearchBrief", f"v{context.get('research_brief_version', '?')}")

    priority = plan.get("priority")
    if priority:
        st.markdown("**Transparent priority score**")
        st.write(
            f"{priority['score']:.3f} = 0.4 × uncertainty reduction "
            f"+ 0.3 × robustness value + 0.2 × safety value − 0.1 × compute cost"
        )
        st.json(priority)

    config = plan.get("config")
    if config:
        with st.expander("Proposed configuration", expanded=True):
            st.json(config)

    hypothesis = plan.get("hypothesis")
    if hypothesis:
        with st.expander("Draft hypothesis and operationalized protocol", expanded=True):
            st.write(hypothesis["original_text"])
            st.json(hypothesis["protocol"])
    if plan.get("hypothesis_id"):
        st.info(f"This suggestion reuses approved hypothesis `{plan['hypothesis_id']}`.")
    if plan.get("critic_report_id"):
        st.info(f"Relevant deterministic critic report: `{plan['critic_report_id']}`.")
    if plan.get("run_id"):
        st.info(f"Completed run awaiting critique: `{plan['run_id']}`.")
    if plan.get("pending_decision_id"):
        st.info(f"Independent work may continue, but this branch waits for decision `{plan['pending_decision_id']}`.")

    current_brief = repository.get_active_brief(project_id)
    snapshots = [
        snapshot
        for snapshot in repository.list_evidence_snapshots(project_id)
        if snapshot["research_brief_version"] == current_brief["version"]
    ]
    snapshot_by_id = {snapshot["id"]: snapshot for snapshot in snapshots}
    snapshot_id = st.selectbox(
        "Attach a same-version evidence snapshot (optional)",
        [None, *snapshot_by_id],
        format_func=lambda value: "No snapshot" if value is None else _snapshot_label(snapshot_by_id[value]),
        key="planner_snapshot_choice",
    )

    if plan.get("decision_draft"):
        if st.button("Save as a pending decision card", key="planner_save_decision"):
            try:
                draft = plan["decision_draft"]
                decision_id = repository.create_decision(
                    draft["question"],
                    draft["decision_type"],
                    draft["options"],
                    project_id,
                    evidence_snapshot_id=snapshot_id,
                )
                st.session_state.pop("scientesis_planner_result", None)
                st.success(f"Saved `{decision_id}` as pending. No option was selected.")
                st.rerun()
            except ValueError as error:
                st.error(str(error))

    if config and plan.get("action") in {
        "establish_baseline",
        "replicate_baseline",
        "test_training_noise",
        "replicate_training_noise",
    }:
        if st.button("Save as an unapproved proposal draft", key="planner_save_proposal"):
            try:
                saved = repository.save_planner_draft(plan, project_id, snapshot_id)
                st.session_state.pop("scientesis_planner_result", None)
                if saved["hypothesis_id"]:
                    st.success(
                        f"Saved proposal `{saved['proposal_id']}` and hypothesis `{saved['hypothesis_id']}` as drafts. "
                        "Review and approve the hypothesis, then separately approve the proposal. No experiment has run."
                    )
                else:
                    st.success(
                        f"Saved `{saved['proposal_id']}` as an unapproved proposal. No experiment has run."
                    )
                st.rerun()
            except (ValueError, KeyError) as error:
                st.error(str(error))

    if plan.get("action") == "run_deterministic_critic" and plan.get("run_id"):
        if st.button("Run deterministic critic", key="planner_run_critic"):
            try:
                report = critique_run(repository, plan["run_id"])
                st.session_state.pop("scientesis_planner_result", None)
                st.success(f"Saved critic report `{report['id']}`. Review it before planning again.")
                st.rerun()
            except (ValueError, KeyError) as error:
                st.error(str(error))


def _snapshot_label(snapshot: dict) -> str:
    detail = snapshot.get("moss_query") or snapshot.get("context_note") or "curated evidence set"
    return f"{snapshot['id']} · brief v{snapshot['research_brief_version']} · {detail[:80]}"
