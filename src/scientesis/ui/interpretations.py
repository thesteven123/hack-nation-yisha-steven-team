from __future__ import annotations

import streamlit as st


VERDICT_LABELS = {
    "accepted_evidence": "Accepted as evidence",
    "preliminary": "Preliminary",
    "inconclusive": "Inconclusive",
    "rejected": "Rejected",
}


def render_interpretation_tab_section(repository, project_id: str, critic_report: dict) -> None:
    st.markdown("### Scientist interpretation")
    st.warning(
        "This is the human judgment, separate from the automatic critic verdict. "
        "Recording it does not change the ResearchBrief or authorize another experiment."
    )
    history = repository.list_human_interpretations(project_id, critic_report["id"])
    current = history[0] if history else None
    verdicts = list(VERDICT_LABELS)
    index = verdicts.index(current["verdict"]) if current else 0
    verdict = st.selectbox(
        "Interpret this finding",
        verdicts,
        index=index,
        format_func=lambda value: VERDICT_LABELS[value],
        key=f"interpretation_verdict_{critic_report['id']}",
    )
    rationale = st.text_area(
        "Human rationale",
        value=current["rationale"] or "" if current else "",
        key=f"interpretation_rationale_{critic_report['id']}",
    )
    if st.button("Record human interpretation", key=f"interpretation_save_{critic_report['id']}"):
        try:
            interpretation_id = repository.save_human_interpretation(
                critic_report_id=critic_report["id"],
                verdict=verdict,
                rationale=rationale,
                project_id=project_id,
            )
            st.success(f"Recorded {interpretation_id} as the latest scientist interpretation.")
            st.rerun()
        except (KeyError, ValueError) as error:
            st.error(str(error))

    if history:
        with st.expander("Interpretation history", expanded=False):
            for item in history:
                label = VERDICT_LABELS.get(item["verdict"], item["verdict"])
                st.markdown(f"**{label} · {item['created_at']}**")
                if item["rationale"]:
                    st.write(item["rationale"])
