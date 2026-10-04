from __future__ import annotations

import streamlit as st

from scientesis.adapters.llm import LLMSettings, OpenAICompatibleClient
from scientesis.services.synthesis import generate_synthesis_draft


def render_synthesis_tab(repository, project_id: str, project_root) -> None:
    st.subheader("LLM-assisted research synthesis")
    st.caption(
        "This optional assistant reads one selected, same-version evidence snapshot and returns a hypothesis and proposal draft. "
        "It has no tools and cannot change the target, answer decisions, approve work, or start training."
    )

    settings = None
    try:
        settings = LLMSettings.from_environment()
    except ValueError as error:
        st.warning(
            f"Synthesis is not configured: {error} Add or change credentials and endpoints in the Settings tab. "
            "No provider call will happen until you explicitly click Generate."
        )

    if settings is not None:
        st.write(f"Configured model: `{settings.model}`")
        st.caption(
            "Clicking Generate sends the active ResearchBrief and only the selected snapshot's records to the configured provider. "
            "Raw uploaded datasets and unrelated project records are excluded."
        )

    brief = repository.get_active_brief(project_id)
    snapshots = [
        snapshot
        for snapshot in repository.list_evidence_snapshots(project_id)
        if snapshot["research_brief_version"] == brief["version"]
    ]
    if not snapshots:
        st.info("Create an evidence snapshot for the active ResearchBrief before generating synthesis.")
        return

    snapshot_by_id = {snapshot["id"]: snapshot for snapshot in snapshots}
    snapshot_id = st.selectbox(
        "Select a current evidence snapshot",
        list(snapshot_by_id),
        format_func=lambda value: _snapshot_label(snapshot_by_id[value]),
        key="llm_synthesis_snapshot",
    )
    selected_snapshot = snapshot_by_id[snapshot_id]
    st.write(
        f"References: {len(selected_snapshot['source_ids'])} approved sources · "
        f"{len(selected_snapshot['experiment_ids'])} experiment runs · "
        f"{len(selected_snapshot['critic_report_ids'])} critic reports · "
        f"{len(selected_snapshot['moss_document_ids'])} Moss search results"
    )
    st.caption("For context and cost control, synthesis accepts at most eight total references per snapshot.")

    draft_key = f"llm_synthesis_draft_{project_id}"
    if st.button("Generate hypothesis and proposal draft", disabled=settings is None, key="llm_synthesis_generate"):
        try:
            draft = generate_synthesis_draft(
                OpenAICompatibleClient(settings), repository, project_id, snapshot_id, project_root
            )
            st.session_state[draft_key] = {
                "draft": draft,
                "snapshot_id": snapshot_id,
                "model": settings.model,
                "brief_version": brief["version"],
            }
            st.success("Draft generated and validated. Nothing has been saved or approved yet.")
        except (ValueError, RuntimeError, KeyError) as error:
            st.error(str(error))

    saved_state = st.session_state.get(draft_key)
    if not isinstance(saved_state, dict):
        return

    is_current = saved_state["brief_version"] == brief["version"] and saved_state["snapshot_id"] in snapshot_by_id
    if not is_current:
        st.warning("This generated draft refers to an older ResearchBrief or snapshot. Generate a fresh draft before saving.")

    draft = saved_state["draft"]
    st.markdown("### Draft hypothesis")
    st.write(draft["hypothesis"]["original_text"])
    with st.expander("Operationalized protocol"):
        st.json(draft["hypothesis"]["protocol"])
    st.markdown("### Draft proposal")
    st.json(draft["proposal"]["config"])
    st.write(draft["proposal"]["rationale"])
    st.markdown("**Limitations**")
    for limitation in draft["limitations"]:
        st.write(f"- {limitation}")
    with st.expander("Snapshot references cited"):
        st.json(draft["evidence_references"])

    if st.button("Save as unapproved hypothesis and proposal drafts", disabled=not is_current, key="llm_synthesis_save"):
        try:
            saved = repository.save_synthesis_draft(
                draft,
                saved_state["snapshot_id"],
                saved_state["model"],
                project_id,
                project_root,
            )
            st.session_state.pop(draft_key, None)
            st.success(
                f"Saved proposal `{saved['proposal_id']}` and hypothesis `{saved['hypothesis_id']}` as unapproved drafts. "
                "Review the hypothesis and separately approve the exact proposal configuration. No experiment has run."
            )
            st.rerun()
        except (ValueError, KeyError) as error:
            st.error(str(error))


def _snapshot_label(snapshot: dict) -> str:
    detail = snapshot.get("moss_query") or snapshot.get("context_note") or "curated evidence set"
    return f"{snapshot['id']} · brief v{snapshot['research_brief_version']} · {detail[:80]}"
