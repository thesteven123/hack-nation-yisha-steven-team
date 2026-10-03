import hashlib

import pytest

from scientesis.db.repository import Repository
from scientesis.services.dataset_intake import (
    inspect_upload,
    parse_expected_ranges,
    save_reviewed_upload,
)


def test_csv_report_preserves_source_and_flags_duplicates_missing_and_ranges():
    content = b"id,score,label\n1,0.5,ok\n2,1.4,\n2,1.4,\n"
    report = inspect_upload(
        "observations.csv",
        content,
        expected_ranges={"score": {"min": 0, "max": 1}},
    )

    assert report["sha256"] == hashlib.sha256(content).hexdigest()
    assert report["row_count"] == 3
    assert report["column_count"] == 3
    assert report["duplicate_rows"] == 1
    assert report["errors"] == []
    score = next(column for column in report["columns"] if column["name"] == "score")
    label = next(column for column in report["columns"] if column["name"] == "label")
    assert score["inferred_type"] == "number"
    assert score["out_of_range_count"] == 2
    assert label["missing_count"] == 2
    assert content == b"id,score,label\n1,0.5,ok\n2,1.4,\n2,1.4,\n"


def test_csv_reports_duplicate_headers_and_malformed_row_widths():
    duplicates = inspect_upload("duplicate.csv", b"x,x\n1,2\n")
    assert any("duplicate column names" in error for error in duplicates["errors"])

    malformed = inspect_upload("wide.csv", b"x,y\n1\n2,3,4\n")
    assert malformed["row_width_mismatches"] == 2
    assert any("field count differs" in warning for warning in malformed["warnings"])


def test_json_profiles_mixed_record_keys_and_rejects_duplicate_object_keys():
    content = b'[{"score": 1}, {"score": 2, "label": "ok"}]'
    report = inspect_upload("records.json", content)
    assert report["row_count"] == 2
    assert any("inconsistent keys" in warning for warning in report["warnings"])
    label = next(column for column in report["columns"] if column["name"] == "label")
    assert label["missing_count"] == 1

    repeated = inspect_upload("repeated.json", b'{"score": 1, "score": 2}')
    assert repeated["duplicate_keys"] == ["score"]
    assert repeated["errors"]


def test_json_column_arrays_and_invalid_shapes():
    report = inspect_upload("columns.json", b'{"x": [1, 2], "y": [3, 4]}')
    assert report["row_count"] == 2
    assert report["column_count"] == 2

    invalid = inspect_upload("uneven.json", b'{"x": [1], "y": [2, 3]}')
    assert any("same number of items" in error for error in invalid["errors"])


def test_markdown_is_kept_as_document_and_has_no_tabular_claims():
    report = inspect_upload("notes.md", b"# Findings\n\nOne paragraph.\n")
    assert report["headings"] == 1
    assert report["row_count"] == 2
    assert report["columns"] == []
    assert any("tabular" in warning for warning in report["warnings"])


def test_expected_ranges_are_validated_and_reject_nonfinite_or_inverted_bounds():
    assert parse_expected_ranges('{"score": {"min": 0, "max": 1}}') == {
        "score": {"min": 0.0, "max": 1.0}
    }
    with pytest.raises(ValueError):
        parse_expected_ranges('{"score": {"min": 2, "max": 1}}')
    with pytest.raises(ValueError):
        parse_expected_ranges('{"score": {"min": 1e999}}')
    with pytest.raises(ValueError):
        parse_expected_ranges("[]")


def test_unsupported_empty_and_oversized_uploads_are_rejected():
    with pytest.raises(ValueError):
        inspect_upload("data.xlsx", b"content")
    with pytest.raises(ValueError):
        inspect_upload("empty.csv", b"")
    with pytest.raises(ValueError):
        inspect_upload("large.csv", b"x" * (10 * 1024 * 1024 + 1))


def test_invalid_utf8_is_reported_without_attempting_cleanup():
    report = inspect_upload("bad.csv", b"x\n\xff")
    assert report["errors"]
    assert report["row_count"] == 0


def test_reviewed_upload_is_stored_byte_for_byte_with_provenance_and_audit(tmp_path):
    repository = Repository(tmp_path / "test.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    content = b"id,score\n1,0.5\n2,0.8\n"
    report = inspect_upload("measurements.csv", content, {"score": {"min": 0, "max": 1}})
    storage_root = tmp_path / "data" / "uploads"

    dataset_id = save_reviewed_upload(
        repository,
        project_id,
        "measurements.csv",
        content,
        report,
        {"collection_method": "Collected in a controlled simulation", "source_notes": "Synthetic"},
        reviewed=True,
        storage_root=storage_root,
        expected_ranges={"score": {"min": 0.0, "max": 1.0}},
    )

    [dataset] = repository.list_datasets(project_id)
    stored_path = storage_root.parent / dataset["storage_uri"]
    assert dataset["id"] == dataset_id
    assert dataset["sha256"] == hashlib.sha256(content).hexdigest()
    assert stored_path.read_bytes() == content
    assert dataset["provenance"]["collection_method"] == "Collected in a controlled simulation"
    assert dataset["validation_report"] == report
    assert any(
        event["entity_id"] == dataset_id and event["action"] == "human_upload_saved_after_review"
        for event in repository.list_audit_events(project_id)
    )


def test_upload_cannot_be_saved_without_confirmation_or_with_stale_report(tmp_path):
    repository = Repository(tmp_path / "test.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    content = b"id\n1\n"
    report = inspect_upload("data.csv", content)
    provenance = {"collection_method": "Survey"}
    storage_root = tmp_path / "uploads"

    with pytest.raises(ValueError, match="Review"):
        save_reviewed_upload(repository, project_id, "data.csv", content, report, provenance, False, storage_root)
    altered_report = {**report, "row_count": 99}
    with pytest.raises(ValueError, match="changed after review"):
        save_reviewed_upload(repository, project_id, "data.csv", content, altered_report, provenance, True, storage_root)
    assert not storage_root.exists()
