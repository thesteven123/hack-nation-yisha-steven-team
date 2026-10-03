from __future__ import annotations

from importlib.util import find_spec
from pathlib import Path

import streamlit as st

from scientesis.adapters.moss import MossAdapter, MossConfigurationError, MossDependencyError, MossOperationError, MossSettings
from scientesis.services.evidence_snapshots import build_moss_documents, snapshot_references_for_hits


def render_retrieval_panel(repository, project_id: str, project_root: str | Path) -> None:
    st.divider()
    st.subheader("Evidence snapshots and Moss retrieval")
    st.caption(
        "Snapshots preserve the exact selected source, experiment, and critic-report IDs together with the active ResearchBrief version. "
        "Snapshots are append-only; creating one does not authorize an experiment or change a claim."
    )
    st.warning(
        "Moss is an external cloud service. Sync sends the current ResearchBrief, approved hypotheses, run summaries, critic reports, "
        "decisions, interpretations, and approved evidence cards. Raw uploaded datasets are excluded. Sync uploads that context; Search sends its query. Neither runs automatically."
    )

    try:
        settings = MossSettings.from_environment()
        moss_configured = True
    except MossConfigurationError as error:
        settings = None
        moss_configured = False
        st.info(str(error))
    moss_installed = find_spec("moss") is not None
    if moss_configured and not moss_installed:
        st.info("Moss credentials are set, but the optional SDK is not installed. Install it with `python -m pip install -e '.[retrieval]'`.")

    sync_col, sync_status = st.columns([1, 2])
    if sync_col.button("Sync approved research context to Moss", disabled=not (moss_configured and moss_installed)):
        try:
            documents = build_moss_documents(repository, project_id, project_root)
            result = MossAdapter(settings=settings).sync_documents(documents)
            sync_status.success(f"Indexed {result['indexed_count']} records in `{result['index_name']}`.")
        except (MossConfigurationError, MossDependencyError, MossOperationError, ValueError, OSError) as error:
            sync_status.error(str(error))

    with st.form("moss_search_form"):
        query = st.text_input("Search the synced research context", max_chars=2_000)
        top_k = st.number_input("Maximum results", min_value=1, max_value=25, value=5, step=1)
        semantic_balance = st.slider("Semantic vs. keyword match", min_value=0.0, max_value=1.0, value=0.8, step=0.1)
        search = st.form_submit_button("Search Moss", disabled=not (moss_configured and moss_installed))
    if search:
        try:
            hits = MossAdapter(settings=settings).search(query, int(top_k), semantic_balance, project_id)
            st.session_state["scientesis_moss_search"] = {"query": query, "hits": hits}
        except (MossConfigurationError, MossDependencyError, MossOperationError, ValueError) as error:
            st.error(str(error))

    search_state = st.session_state.get("scientesis_moss_search")
    if search_state:
        hits = search_state["hits"]
        if not hits:
            st.info("Moss returned no project-scoped matches for this query.")
        else:
            hit_by_id = {hit["id"]: hit for hit in hits}
            document_options = list(hit_by_id)
            selected_document_ids = st.multiselect(
                "Select the exact retrieved records to preserve",
                document_options,
                default=document_options,
                format_func=lambda value: _hit_label(hit_by_id[value]),
                key="scientesis_moss_snapshot_selection",
            )
            for document_id in selected_document_ids:
                hit = hit_by_id[document_id]
                with st.expander(_hit_label(hit)):
                    st.text(hit["text"][:4_000])
            if st.button("Create immutable snapshot from selected results", key="save-moss-snapshot"):
                refs = snapshot_references_for_hits(hits, selected_document_ids, hits)
                try:
                    snapshot = repository.create_evidence_snapshot(
                        project_id=project_id,
                        moss_query=search_state["query"],
                        moss_document_ids=refs["moss_document_ids"],
                        retrieved_hits=[hit for hit in hits if hit["id"] in refs["moss_document_ids"]],
                        source_ids=refs["source_ids"],
                        experiment_ids=refs["experiment_ids"],
                        critic_report_ids=refs["critic_report_ids"],
                    )
                    st.session_state.pop("scientesis_moss_search", None)
                    st.session_state.pop("scientesis_moss_snapshot_selection", None)
                    st.success(f"Saved immutable snapshot `{snapshot['id']}` for ResearchBrief v{snapshot['research_brief_version']}.")
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))

    st.markdown("### Create a manual evidence snapshot")
    approved_cards = repository.list_evidence_cards(project_id, approval_status="approved")
    finished_runs = [run for run in repository.list_runs(project_id) if run["status"] in {"completed", "failed"}]
    critic_reports = repository.list_critic_reports(project_id)
    with st.form("manual_evidence_snapshot_form"):
        context_note = st.text_area("Why are these records being considered? (optional)", max_chars=2_000)
        source_ids = st.multiselect("Approved evidence cards", [card["id"] for card in approved_cards])
        experiment_ids = st.multiselect("Completed or failed experiment runs", [run["id"] for run in finished_runs])
        critic_report_ids = st.multiselect("Critic reports", [report["id"] for report in critic_reports])
        create_manual_snapshot = st.form_submit_button("Save manual snapshot")
    if create_manual_snapshot:
        try:
            snapshot = repository.create_evidence_snapshot(
                project_id=project_id,
                source_ids=source_ids,
                experiment_ids=experiment_ids,
                critic_report_ids=critic_report_ids,
                context_note=context_note,
            )
            st.success(f"Saved immutable snapshot `{snapshot['id']}` for ResearchBrief v{snapshot['research_brief_version']}.")
            st.rerun()
        except (ValueError, KeyError) as error:
            st.error(str(error))

    snapshots = repository.list_evidence_snapshots(project_id)
    st.markdown("### Saved snapshots")
    if not snapshots:
        st.caption("No evidence snapshots yet.")
    for snapshot in snapshots:
        detail = snapshot["moss_query"] or snapshot.get("context_note") or "Manual evidence set"
        with st.expander(f"{snapshot['id']} · ResearchBrief v{snapshot['research_brief_version']} · {detail[:100]}"):
            st.write(
                {
                    "source_ids": snapshot["source_ids"],
                    "experiment_ids": snapshot["experiment_ids"],
                    "critic_report_ids": snapshot["critic_report_ids"],
                    "moss_document_ids": snapshot["moss_document_ids"],
                    "created_at": snapshot["created_at"],
                }
            )
            if snapshot["context_note"]:
                st.text(snapshot["context_note"])
            for hit in snapshot["retrieved_hits"]:
                metadata = hit.get("metadata") or {}
                with st.expander(f"{metadata.get('record_type', 'record').replace('_', ' ')} · {hit['id']} · {hit['score']:.3f}"):
                    st.text(hit["text"])


def _hit_label(hit: dict) -> str:
    metadata = hit.get("metadata") or {}
    record_type = metadata.get("record_type", "record").replace("_", " ")
    return f"{record_type} · {hit['id']} · relevance {hit['score']:.3f}"
