from __future__ import annotations

import streamlit as st


def render_research_target_tab(repository, project_id: str) -> None:
    brief = repository.get_active_brief(project_id)
    st.subheader("Research target and protocol")
    st.caption(
        f"ResearchBrief v{brief['version']} · edits create an immutable new version and make older proposals stale. "
        "This MVP is fixed to Reacher-v5 with PPO."
    )
    with st.expander("Edit ResearchBrief", expanded=False):
        with st.form("research_brief_form"):
            project_title = st.text_input("Project title", value=brief["project_title"])
            research_question = st.text_area("Research question", value=brief["research_question"])
            objective = st.text_area("Objective", value=brief["objective"])
            primary_outcome = st.text_input("Primary outcome", value=brief["primary_outcome"])
            secondary_outcomes = st.text_area(
                "Secondary outcomes (one per line)", value="\n".join(brief["secondary_outcomes"])
            )
            notes = st.text_area("Scope and limitations", value=brief["notes"])
            reason = st.text_input("Why are you changing the target?")

            st.markdown("**Experiment limits**")
            experiment_budget = st.number_input("Total experiment budget", min_value=1, max_value=100, value=brief["experiment_budget"])
            training_steps = st.number_input(
                "Training steps per run", min_value=1000, max_value=brief["max_training_steps_per_run"], step=1000,
                value=brief["training_steps_per_run"],
            )
            evaluation_episodes = st.number_input("Evaluation episodes", min_value=1, max_value=1000, value=brief["evaluation_episodes"])
            minimum_seeds = st.number_input("Independent seeds required for a claim", min_value=1, max_value=30, value=brief["minimum_seeds_for_claim"])
            training_seeds = st.text_input("Training seeds (comma-separated)", value=", ".join(map(str, brief["training_seeds"])))

            st.markdown("**Permitted experiment settings**")
            noise_values = st.text_input("Allowed observation-noise values", value=", ".join(map(str, brief["allowed_noise_values"])))
            safety_penalties = st.text_input("Allowed safety-proxy penalties", value=", ".join(map(str, brief["allowed_safety_penalties"])))
            success_distance = st.number_input("Success-distance threshold", min_value=0.001, max_value=2.0, value=float(brief["success_distance_threshold"]), format="%.3f")
            noise_improvement = st.number_input("Minimum noisy success improvement", min_value=0.0, max_value=1.0, value=float(brief["minimum_noisy_success_improvement"]), format="%.3f")
            clean_decline = st.number_input("Maximum clean success decline", min_value=0.0, max_value=1.0, value=float(brief["max_clean_success_decline"]), format="%.3f")
            saturation_threshold = st.number_input("Actuator saturation threshold", min_value=0.0, max_value=1.0, value=float(brief["actuator_saturation_threshold"]), format="%.3f")
            safety_tolerance = st.number_input("Safety-proxy regression tolerance", min_value=0.0, max_value=1.0, value=float(brief["safety_regression_tolerance"]), format="%.3f")
            safety_definition = st.text_area("Safety metric definition", value=brief["safety_metric_definition"])
            saved = st.form_submit_button("Save as a new ResearchBrief version")

        if saved:
            try:
                candidate = dict(brief)
                candidate.update(
                    {
                        "project_title": project_title,
                        "research_question": research_question,
                        "objective": objective,
                        "primary_outcome": primary_outcome,
                        "secondary_outcomes": _lines(secondary_outcomes),
                        "notes": notes,
                        "experiment_budget": int(experiment_budget),
                        "training_steps_per_run": int(training_steps),
                        "evaluation_episodes": int(evaluation_episodes),
                        "minimum_seeds_for_claim": int(minimum_seeds),
                        "training_seeds": _integers(training_seeds),
                        "allowed_noise_values": _numbers(noise_values),
                        "allowed_safety_penalties": _numbers(safety_penalties),
                        "success_distance_threshold": float(success_distance),
                        "minimum_noisy_success_improvement": float(noise_improvement),
                        "max_clean_success_decline": float(clean_decline),
                        "actuator_saturation_threshold": float(saturation_threshold),
                        "safety_regression_tolerance": float(safety_tolerance),
                        "safety_metric_definition": safety_definition,
                    }
                )
                updated = repository.update_research_brief(candidate, reason, project_id)
                st.success(f"Saved ResearchBrief v{updated['version']}.")
                st.rerun()
            except ValueError as error:
                st.error(str(error))

    with st.expander("Write a hypothesis and protocol", expanded=False):
        with st.form("hypothesis_form"):
            original_text = st.text_area("Hypothesis")
            treatment = st.text_area("Treatment condition")
            control = st.text_area("Control condition")
            outcome = st.text_area("Primary outcome")
            submitted = st.form_submit_button("Save as draft for human review")
        if submitted:
            protocol = {
                "environment": brief["environment"],
                "algorithm": brief["algorithm"],
                "treatment": treatment,
                "control": control,
                "primary_outcome": outcome,
                "seed_protocol": brief["training_seeds"],
            }
            try:
                hypothesis_id = repository.create_hypothesis(original_text, protocol, project_id)
                st.success(f"Saved {hypothesis_id} as a draft. It cannot be linked to a proposal until approved.")
                st.rerun()
            except ValueError as error:
                st.error(str(error))

    hypotheses = repository.list_hypotheses(project_id)
    drafts = [hypothesis for hypothesis in hypotheses if hypothesis["status"] == "draft"]
    if drafts:
        st.markdown("### Hypothesis review queue")
        for hypothesis in drafts:
            with st.container(border=True):
                st.markdown(f"**{hypothesis['id']} · Draft · ResearchBrief v{hypothesis['brief_version']}**")
                st.write(hypothesis["original_text"])
                st.json(hypothesis["protocol"])
                note = st.text_input("Review note", key=f"hypothesis_note_{hypothesis['id']}")
                left, right = st.columns(2)
                if left.button("Approve protocol", key=f"hypothesis_approve_{hypothesis['id']}"):
                    try:
                        repository.review_hypothesis(hypothesis["id"], "approved", note)
                        st.success("Approved. Proposals can now link this hypothesis while its ResearchBrief version is active.")
                        st.rerun()
                    except ValueError as error:
                        st.error(str(error))
                if right.button("Reject protocol", key=f"hypothesis_reject_{hypothesis['id']}"):
                    try:
                        repository.review_hypothesis(hypothesis["id"], "rejected", note)
                        st.info("Rejected. It remains in the project history and cannot be linked to a proposal.")
                        st.rerun()
                    except ValueError as error:
                        st.error(str(error))

    if hypotheses:
        with st.expander("Hypothesis history"):
            for hypothesis in hypotheses:
                st.markdown(f"**{hypothesis['id']} · {hypothesis['status'].title()} · ResearchBrief v{hypothesis['brief_version']}**")
                st.write(hypothesis["original_text"])
                if hypothesis["review_note"]:
                    st.caption(hypothesis["review_note"])

    st.markdown("### ResearchBrief version history")
    for version in repository.list_brief_versions(project_id):
        st.markdown(f"**v{version['version']} · {version['created_at']} · {version['created_by']}** — {version['change_reason']}")


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _integers(value: str) -> list[int]:
    try:
        return [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as error:
        raise ValueError("Training seeds must be comma-separated integers.") from error


def _numbers(value: str) -> list[float]:
    try:
        return [float(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as error:
        raise ValueError("Allowed values must be comma-separated numbers.") from error
