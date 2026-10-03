from __future__ import annotations

import re

import streamlit as st


def render_decision_tab(repository, project_id: str) -> None:
    st.subheader("Decision center")
    st.caption(
        "Decisions are durable records. Choosing an option applies only to this decision; it does not change the ResearchBrief or authorize an experiment. "
        "Deferring leaves the decision pending."
    )
    with st.expander("Create a decision card", expanded=False):
        option_count = st.selectbox("Number of alternatives", [2, 3, 4], index=2, key="decision_option_count")
        with st.form("decision_create_form"):
            question = st.text_area("Concrete question")
            decision_type = st.selectbox(
                "Decision type",
                ["research_direction", "protocol_change", "result_interpretation", "other"],
            )
            options = []
            for index in range(option_count):
                st.markdown(f"**Alternative {index + 1}**")
                label = st.text_input("Label", key=f"decision_label_{index}")
                rationale = st.text_area("Rationale", key=f"decision_rationale_{index}")
                benefits_text = st.text_area("Benefits (one per line)", key=f"decision_benefits_{index}")
                risks_text = st.text_area("Risks (one per line)", key=f"decision_risks_{index}")
                cost_left, cost_right = st.columns(2)
                experiments = cost_left.number_input(
                    "Estimated experiments", min_value=0, step=1, key=f"decision_cost_exp_{index}"
                )
                training_steps = cost_right.number_input(
                    "Estimated training steps", min_value=0, step=1000, key=f"decision_cost_steps_{index}"
                )
                evidence_text = st.text_input(
                    "Evidence IDs (comma-separated)", key=f"decision_evidence_{index}"
                )
                flag_left, flag_right = st.columns(2)
                recommended = flag_left.checkbox("Mark as recommended", key=f"decision_recommended_{index}")
                modify_target = flag_right.checkbox(
                    "This option modifies the research target", key=f"decision_modify_{index}"
                )
                options.append(
                    {
                        "label": label,
                        "rationale": rationale,
                        "benefits": _lines(benefits_text),
                        "risks": _lines(risks_text),
                        "cost_estimate": {"experiments": experiments, "training_steps": training_steps},
                        "evidence_refs": _comma_items(evidence_text),
                        "is_recommended": recommended,
                        "is_modify_target_option": modify_target,
                    }
                )
            submitted = st.form_submit_button("Save pending decision")
        if submitted:
            try:
                decision_id = repository.create_decision(
                    question=question,
                    decision_type=decision_type,
                    options=options,
                    project_id=project_id,
                )
                st.success(f"Saved {decision_id} as pending. No answer has been inferred.")
                st.rerun()
            except ValueError as error:
                st.error(str(error))

    decisions = repository.list_decisions(project_id)
    pending = [decision for decision in decisions if decision["status"] == "pending"]
    if pending:
        st.markdown("### Pending decisions")
        for decision in pending:
            _render_pending_decision(repository, decision)
    else:
        st.caption("No pending decisions.")

    answered = [decision for decision in decisions if decision["status"] != "pending"]
    if answered:
        st.markdown("### Answer history")
        for decision in answered:
            with st.expander(f"{decision['id']} · {decision['status'].title()} · {decision['question']}"):
                for answer in decision["answers"]:
                    st.write(f"**{answer['answer_type'].replace('_', ' ').title()}** · {answer['created_at']}")
                    if answer["selected_option_id"]:
                        selected = next(
                            (option["label"] for option in decision["options"] if option["id"] == answer["selected_option_id"]),
                            answer["selected_option_id"],
                        )
                        st.write(f"Selected: {selected}")
                    if answer["custom_response"]:
                        st.write(answer["custom_response"])
                    if answer["rationale_optional"]:
                        st.caption(answer["rationale_optional"])
                    st.caption(f"Scope: {answer['scope'].get('applies_to', 'unspecified')}")


def _render_pending_decision(repository, decision: dict) -> None:
    with st.container(border=True):
        st.markdown(f"**{decision['id']} · {decision['decision_type'].replace('_', ' ').title()}**")
        st.markdown(f"### {decision['question']}")
        st.caption(f"Pending · ResearchBrief v{decision['pre_brief_version']} · no response is recorded as agreement.")
        for index, option in enumerate(decision["options"], start=1):
            marker = " · **Recommended**" if option["is_recommended"] else ""
            modify = " · **Modify target**" if option["is_modify_target_option"] else ""
            st.markdown(f"#### {index}. {option['label']}{marker}{modify}")
            st.write(option["rationale"])
            left, right = st.columns(2)
            with left:
                st.markdown("**Benefits**")
                _show_items(option["benefits"])
            with right:
                st.markdown("**Risks**")
                _show_items(option["risks"])
            cost = option["cost_estimate"]
            st.caption(
                f"Estimated cost · {cost.get('experiments', 0)} experiment(s) · "
                f"{cost.get('training_steps', 0):,} training steps"
            )
            st.caption("Evidence: " + (", ".join(option["evidence_refs"]) if option["evidence_refs"] else "None recorded"))
        option_ids = [option["id"] for option in decision["options"]]
        labels = {option["id"]: option["label"] for option in decision["options"]}
        selected = st.radio(
            "Select an alternative (optional)",
            option_ids,
            format_func=lambda option_id: labels[option_id],
            key=f"decision_choice_{decision['id']}",
        )
        custom_response = st.text_area("Custom direction (optional)", key=f"decision_custom_{decision['id']}")
        rationale = st.text_area("Your rationale (optional)", key=f"decision_answer_rationale_{decision['id']}")
        applies_to = st.text_input(
            "Scope of this answer",
            value=f"Decision {decision['id']} only",
            key=f"decision_scope_{decision['id']}",
        )
        scope = {"applies_to": applies_to, "creates_permanent_preference": False}
        left, middle, right = st.columns(3)
        if left.button("Record selected option", key=f"decision_submit_choice_{decision['id']}"):
            try:
                repository.answer_decision(
                    decision["id"], "option_selected", scope,
                    selected_option_id=selected, rationale=rationale,
                )
                st.success("Choice recorded for this decision only. The brief and experiment permissions are unchanged.")
                st.rerun()
            except ValueError as error:
                st.error(str(error))
        if middle.button("Record custom direction", key=f"decision_submit_custom_{decision['id']}"):
            try:
                repository.answer_decision(
                    decision["id"], "custom_text", scope,
                    custom_response=custom_response, rationale=rationale,
                )
                st.success("Custom direction recorded for this decision only.")
                st.rerun()
            except ValueError as error:
                st.error(str(error))
        if right.button("Not deciding yet", key=f"decision_defer_{decision['id']}"):
            try:
                repository.answer_decision(
                    decision["id"], "defer", scope, rationale=rationale,
                )
                st.info("Deferral recorded; the decision remains pending.")
                st.rerun()
            except ValueError as error:
                st.error(str(error))


def _show_items(items: list[str]) -> None:
    if items:
        for item in items:
            st.write(f"- {item}")
    else:
        st.caption("None recorded")


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _comma_items(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]
