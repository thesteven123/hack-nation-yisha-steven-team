from __future__ import annotations

import hashlib
import uuid

import streamlit as st

from scientesis.services.dataset_intake import (
    MAX_UPLOAD_BYTES,
    inspect_upload,
    parse_expected_ranges,
    save_reviewed_upload,
)


def render_dataset_tab(repository, project_id: str) -> None:
    st.subheader("Add a dataset for human-reviewed research")
    st.caption(
        f"CSV, JSON, or Markdown · up to {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB · original bytes are stored unchanged. "
        "This MVP does not clean, transform, or send uploaded files to an external service."
    )
    uploaded = st.file_uploader(
        "Choose a source file",
        type=["csv", "json", "md", "markdown"],
        key="scientesis_dataset_upload",
    )
    if uploaded is None:
        _show_datasets(repository, project_id)
        return

    content = uploaded.getvalue()
    current_sha = hashlib.sha256(content).hexdigest()
    collection_method = st.text_area(
        "How was this data collected?",
        placeholder="Describe the source, collection process, and any relevant limits.",
        key="scientesis_collection_method",
    )
    source_notes = st.text_area(
        "Source or license notes (optional)",
        key="scientesis_source_notes",
    )
    range_text = st.text_area(
        "Expected numeric ranges (optional JSON)",
        value="{}",
        help='Example: {"score": {"min": 0, "max": 1}}',
        key="scientesis_expected_ranges",
    )

    if st.button("Inspect file", key="scientesis_inspect_dataset"):
        try:
            ranges = parse_expected_ranges(range_text)
            report = inspect_upload(uploaded.name, content, expected_ranges=ranges)
            st.session_state["scientesis_dataset_review"] = {
                "review_id": uuid.uuid4().hex,
                "filename": uploaded.name,
                "sha256": current_sha,
                "report": report,
                "expected_ranges": ranges,
                "range_text": range_text,
                "collection_method": collection_method,
                "source_notes": source_notes,
            }
        except ValueError as error:
            st.error(str(error))

    review = st.session_state.get("scientesis_dataset_review")
    if review is not None:
        if review["filename"] != uploaded.name or review["sha256"] != current_sha:
            st.warning("The selected file changed after inspection. Inspect it again before saving.")
        elif review["collection_method"] != collection_method or review["source_notes"] != source_notes or review["range_text"] != range_text:
            st.warning("The provenance or range settings changed after inspection. Inspect again to refresh the review.")
        else:
            _show_report(review["report"])
            has_errors = bool(review["report"].get("errors"))
            reviewed = st.checkbox(
                "I reviewed this inspection report and confirm the unchanged source may be saved.",
                key=f"scientesis_dataset_reviewed_{review['review_id']}",
                disabled=has_errors,
            )
            if has_errors:
                st.error("Fix the blocking file issue and inspect the corrected file before saving.")
            elif st.button("Save reviewed dataset", key="scientesis_save_dataset"):
                try:
                    dataset_id = save_reviewed_upload(
                        repository=repository,
                        project_id=project_id,
                        filename=uploaded.name,
                        content=content,
                        report=review["report"],
                        provenance={
                            "collection_method": collection_method,
                            "source_notes": source_notes,
                        },
                        reviewed=reviewed,
                        expected_ranges=review["expected_ranges"],
                    )
                    del st.session_state["scientesis_dataset_review"]
                    st.success(f"Saved dataset {dataset_id}. The original file was not modified.")
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))

    _show_datasets(repository, project_id)


def _show_report(report: dict) -> None:
    st.markdown("#### Inspection report")
    st.write(
        f"**{report['row_count']:,} rows** · **{report['column_count']:,} columns** · "
        f"{report['size_bytes']:,} bytes · SHA-256 `{report['sha256']}`"
    )
    if report.get("columns"):
        st.dataframe(report["columns"], use_container_width=True, hide_index=True)
    for warning in report.get("warnings", []):
        st.warning(warning)
    for error in report.get("errors", []):
        st.error(error)
    if report.get("duplicate_rows"):
        st.caption(f"Duplicate rows flagged: {report['duplicate_rows']}; none were removed.")


def _show_datasets(repository, project_id: str) -> None:
    datasets = repository.list_datasets(project_id)
    if not datasets:
        st.caption("No datasets have been saved for this project.")
        return
    st.markdown("#### Saved datasets")
    rows = []
    for dataset in datasets:
        report = dataset["validation_report"]
        rows.append(
            {
                "Name": dataset["name"],
                "Rows": report.get("row_count"),
                "Columns": report.get("column_count"),
                "SHA-256": dataset["sha256"],
                "Collected by": dataset["uploaded_by"],
                "Method": dataset["provenance"].get("collection_method", ""),
                "Saved": dataset["uploaded_at"],
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)
