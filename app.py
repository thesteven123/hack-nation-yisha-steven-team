from __future__ import annotations

from datetime import datetime
from pathlib import Path

import streamlit as st

from scientesis.db import Repository
from scientesis.domain.models import ExperimentConfig
from scientesis.rl.runner import run_experiment
from scientesis.services.critic import critique_run
from scientesis.ui.datasets import render_dataset_tab
from scientesis.ui.evidence import render_evidence_tab
from scientesis.ui.retrieval import render_retrieval_panel

from scientesis.ui.decisions import render_decision_tab
from scientesis.ui.interpretations import render_interpretation_tab_section
from scientesis.ui.research_targets import render_research_target_tab


st.set_page_config(page_title="Scientesis", page_icon="🔬", layout="wide")
repository = Repository()
repository.initialize()
project_id = repository.get_project_id()
brief = repository.get_active_brief(project_id)
PROJECT_ROOT = Path(__file__).resolve().parent
ARTIFACTS_DIR = PROJECT_ROOT / "artifacts"

st.title("Scientesis")
st.caption("Human-guided research lab · reproducible RL experiments · simulation only")

overview_tab, target_tab, proposal_tab, results_tab, dataset_tab, evidence_tab, decision_tab, notebook_tab = st.tabs(
    ["Research overview", "Research target", "Propose & approve", "Runs & critique", "Data intake", "Evidence", "Decision center", "Lab notebook"]
)

with overview_tab:
    st.subheader(brief["project_title"])
    st.markdown(f"**Research question**  \n{brief['research_question']}")
    left, middle, right = st.columns(3)
    left.metric("ResearchBrief", f"v{brief['version']}")
    middle.metric("Training budget", f"{brief['training_steps_per_run']:,} steps/run")
    right.metric("Claim threshold", f"{brief['minimum_seeds_for_claim']} independent seeds")
    st.markdown("**Allowed training/evaluation noise:** " + ", ".join(f"{value:.2f}" for value in brief["allowed_noise_values"]))
    st.markdown("**Allowed safety penalties:** " + ", ".join(str(value) for value in brief["allowed_safety_penalties"]))
    st.info(
        "Safety is currently measured as normalized actuator commands reaching 95% of their limit. "
        "That is a simulation proxy, not evidence of physical-robot safety."
    )
    st.caption(f"Database: `{repository.database_path}` · Only explicitly approved experiments can run.")

with target_tab:
    render_research_target_tab(repository, project_id)

with proposal_tab:
    st.subheader("Create a bounded experiment proposal")
    st.write("Proposals are saved first. They will not execute until you explicitly approve and then start them.")
    approved_hypotheses = repository.list_hypotheses(project_id, status="approved")
    hypothesis_by_id = {hypothesis["id"]: hypothesis for hypothesis in approved_hypotheses}
    current_snapshots = [snapshot for snapshot in repository.list_evidence_snapshots(project_id) if snapshot["research_brief_version"] == brief["version"]]
    snapshot_by_id = {snapshot["id"]: snapshot for snapshot in current_snapshots}
    with st.form("proposal_form"):
        snapshot_options = [None, *snapshot_by_id]
        evidence_snapshot_id = st.selectbox(
            "Evidence snapshot (optional)",
            snapshot_options,
            format_func=lambda value: "No evidence snapshot" if value is None else f"{value} · {snapshot_by_id[value]['moss_query'] or snapshot_by_id[value].get('context_note') or 'manual evidence set'}",
        )
        hypothesis_options = [None, *hypothesis_by_id]
        hypothesis_id = st.selectbox(
            "Approved hypothesis (optional)",
            hypothesis_options,
            format_func=lambda value: "Exploratory proposal (no linked hypothesis)" if value is None else f"{value}: {hypothesis_by_id[value]['original_text'][:90]}",
        )
        noise_train = st.selectbox("Training observation noise", brief["allowed_noise_values"], format_func=lambda value: f"{value:.2f}")
        noise_eval = st.selectbox("Noisy evaluation level", brief["allowed_noise_values"], index=min(2, len(brief["allowed_noise_values"]) - 1), format_func=lambda value: f"{value:.2f}")
        safety_penalty = st.selectbox("Training safety-proxy penalty", brief["allowed_safety_penalties"], format_func=lambda value: f"{value:g}")
        seed = st.selectbox("Training seed", brief["training_seeds"])
        submitted = st.form_submit_button("Save proposal")
    if submitted:
        config = ExperimentConfig.from_brief(brief, noise_train, noise_eval, safety_penalty, seed).to_dict()
        try:
            proposal_id = repository.create_proposal(config, project_id, hypothesis_id=hypothesis_id, evidence_snapshot_id=evidence_snapshot_id)
            st.success(f"Saved {proposal_id} as proposed. No experiment has run.")
        except ValueError as error:
            st.error(str(error))

    st.divider()
    st.subheader("Human review queue")
    proposals = repository.list_proposals(project_id)
    if not proposals:
        st.caption("No proposals yet.")
    for proposal in proposals:
        config = proposal["config"]
        with st.container(border=True):
            hypothesis_label = f" · hypothesis {proposal['hypothesis_id']}" if proposal.get("hypothesis_id") else " · exploratory"
            snapshot_label = f" · snapshot {proposal['evidence_snapshot_id']}" if proposal.get("evidence_snapshot_id") else ""
            st.markdown(f"**{proposal['id']} · {proposal['status'].replace('_', ' ').title()}{hypothesis_label}{snapshot_label}**")
            st.write(
                f"Train noise {config['noise_train']:.2f} · eval noise {config['noise_eval']:.2f} · "
                f"safety penalty {config['safety_penalty']:g} · seed {config['seed']} · "
                f"{config['training_steps']:,} training steps · brief v{proposal['research_brief_version']}"
            )
            if proposal["status"] == "proposed":
                note = st.text_input("Approval note (optional)", key=f"note-{proposal['id']}")
                if st.button("Approve this exact configuration", key=f"approve-{proposal['id']}"):
                    try:
                        repository.approve_proposal(proposal["id"], note)
                        st.success("Approved. Execution is still a separate action.")
                        st.rerun()
                    except ValueError as error:
                        st.error(str(error))
            elif proposal["status"] == "approved":
                if st.button("Start approved experiment", key=f"run-{proposal['id']}"):
                    run_id = None
                    try:
                        run_id = repository.reserve_run(proposal["id"])
                        run_record = repository.start_run(run_id)
                        approved_config = run_record["config"]
                        with st.status(f"Running {run_id} — PPO training and evaluation…", expanded=True) as status:
                            st.write("Training only the approved configuration. This may take several minutes.")
                            result = run_experiment(approved_config, run_id, ARTIFACTS_DIR)
                            repository.complete_run(
                                run_id,
                                result["metrics"],
                                result["artifact_manifest"],
                                result["versions"],
                            )
                            critic = critique_run(repository, run_id)
                            status.update(label=f"{run_id} completed · {critic['verdict']}", state="complete")
                        st.success(f"Completed {run_id}; critic verdict: {critic['verdict']}.")
                        st.rerun()
                    except Exception as error:
                        if run_id:
                            repository.fail_run(run_id, str(error))
                        st.exception(error)
            elif proposal["status"] == "queued":
                st.caption("This proposal has a queued run. Check Runs & critique for its status.")

with results_tab:
    st.subheader("Experiment runs")
    runs = repository.list_runs(project_id)
    if not runs:
        st.caption("No runs yet. Create a proposal, approve it, then start it explicitly.")
    else:
        table = []
        for run in runs:
            metrics = run["metrics"] or {}
            table.append({
                "Run": run["id"],
                "Status": run["status"],
                "Seed": run["seed"],
                "Train noise": run["config"]["noise_train"],
                "Noisy success": metrics.get("noisy_success_rate"),
                "Clean success": metrics.get("clean_success_rate"),
                "Saturation events/episode": metrics.get("noisy_safety_violations_per_episode"),
                "Environment version": run["environment_version"],
            })
        st.dataframe(table, use_container_width=True, hide_index=True)
        failed = [run for run in runs if run["status"] == "failed"]
        for run in failed:
            with st.expander(f"{run['id']} error"):
                st.code(run["error_message"] or "No error details stored.")
    critic_reports = repository.list_critic_reports(project_id)
    latest_critic = critic_reports[0] if critic_reports else None
    if latest_critic:
        st.divider()
        st.subheader(f"Latest critic verdict: {latest_critic['verdict'].replace('_', ' ').title()}")
        for finding in latest_critic["findings"]:
            st.write(f"- {finding}")
        st.markdown("**Limitations**")
        for limitation in latest_critic["limitations"]:
            st.write(f"- {limitation}")
        st.info(latest_critic["recommended_next_action"]["reason"])
        st.caption("The recommended next action is not authorization; a human must approve new work.")
        render_interpretation_tab_section(repository, project_id, latest_critic)

with dataset_tab:
    render_dataset_tab(repository, project_id)

with evidence_tab:
    render_evidence_tab(repository, project_id)
    render_retrieval_panel(repository, project_id, PROJECT_ROOT)

with decision_tab:
    render_decision_tab(repository, project_id)

with notebook_tab:
    st.subheader("Append-only audit trail")
    events = repository.list_audit_events(project_id)
    if not events:
        st.caption("No audit events yet.")
    for event in events:
        payload = event["payload"]
        timestamp = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        st.markdown(f"**{timestamp} · {event['actor_type']} · {event['action']}** — {event['entity_type']} `{event['entity_id']}`")
        if payload:
            with st.expander("Recorded details"):
                st.json(payload)
