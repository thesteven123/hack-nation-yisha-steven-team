import pytest

from scientesis.db.repository import Repository


def option(label, *, modify_target=False, recommended=False):
    return {
        "label": label,
        "rationale": f"Rationale for {label}.",
        "benefits": ["Reduces uncertainty"],
        "risks": ["Uses experiment budget"],
        "cost_estimate": {"experiments": 1, "training_steps": 100000},
        "evidence_refs": ["EXP-001"],
        "is_recommended": recommended,
        "is_modify_target_option": modify_target,
    }


def make_decision(repo):
    return repo.create_decision(
        question="Which research direction should be prioritized next?",
        decision_type="research_direction",
        options=[option("Replicate baseline"), option("Test safety penalty"), option("Modify target", modify_target=True)],
    )


def test_decision_is_persisted_with_visible_options_and_audit(tmp_path):
    repo = Repository(tmp_path / "test.sqlite3")
    repo.initialize()
    decision_id = make_decision(repo)

    decision = repo.get_decision(decision_id)
    assert decision["status"] == "pending"
    assert decision["pre_brief_version"] == 1
    assert len(decision["options"]) == 3
    assert decision["options"][0]["cost_estimate"]["training_steps"] == 100000
    assert decision["options"][2]["is_modify_target_option"] is True
    assert decision["answers"] == []
    assert any(event["entity_id"] == decision_id and event["action"] == "created_pending" for event in repo.list_audit_events())


def test_defer_records_no_choice_and_leaves_decision_pending(tmp_path):
    repo = Repository(tmp_path / "test.sqlite3")
    repo.initialize()
    decision_id = make_decision(repo)

    repo.answer_decision(
        decision_id,
        "defer",
        {"applies_to": f"decision {decision_id} only", "creates_permanent_preference": False},
        rationale="I want to review the next results first.",
    )

    decision = repo.get_decision(decision_id)
    assert decision["status"] == "pending"
    assert decision["answers"][0]["answer_type"] == "defer"
    assert decision["answers"][0]["selected_option_id"] is None
    assert decision["answers"][0]["preference_source"] == "explicit_human"


def test_selection_is_limited_to_the_recorded_decision_scope(tmp_path):
    repo = Repository(tmp_path / "test.sqlite3")
    repo.initialize()
    decision_id = make_decision(repo)
    selected = repo.get_decision(decision_id)["options"][0]["id"]

    repo.answer_decision(
        decision_id,
        "option_selected",
        {"applies_to": f"decision {decision_id} only", "creates_permanent_preference": False},
        selected_option_id=selected,
    )

    decision = repo.get_decision(decision_id)
    assert decision["status"] == "answered"
    assert decision["answers"][0]["selected_option_id"] == selected
    assert decision["answers"][0]["scope"]["applies_to"] == f"decision {decision_id} only"
    with pytest.raises(ValueError, match="pending"):
        repo.answer_decision(
            decision_id,
            "defer",
            {"applies_to": f"decision {decision_id} only", "creates_permanent_preference": False},
        )


def test_invalid_drafts_and_unscoped_or_foreign_answers_are_rejected(tmp_path):
    repo = Repository(tmp_path / "test.sqlite3")
    repo.initialize()
    with pytest.raises(ValueError, match="modify research target"):
        repo.create_decision(
            "Choose next experiment",
            "research_direction",
            [option("A"), option("B")],
        )

    decision_id = make_decision(repo)
    other_id = make_decision(repo)
    other_option = repo.get_decision(other_id)["options"][0]["id"]
    with pytest.raises(ValueError, match="does not belong"):
        repo.answer_decision(
            decision_id,
            "option_selected",
            {"applies_to": f"decision {decision_id} only", "creates_permanent_preference": False},
            selected_option_id=other_option,
        )
    with pytest.raises(ValueError, match="permanent preference"):
        repo.answer_decision(
            decision_id,
            "defer",
            {"applies_to": "global", "creates_permanent_preference": True},
        )
