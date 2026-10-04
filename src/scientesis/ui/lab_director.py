from __future__ import annotations

from pathlib import Path

import streamlit as st

from scientesis.adapters.elevenlabs import ElevenLabsClient, ElevenLabsSettings
from scientesis.services.narration import build_lab_summary, create_narration_audio, load_narration_audio


def render_lab_director_section(repository, project_id: str, project_root: Path) -> None:
    st.divider()
    st.subheader("Lab Director · captured research summary")
    st.caption("Stored metrics, critic verdict, and scientist interpretation stay separate. This summary cannot authorize experiments.")
    completed = repository.list_completed_runs(project_id)
    if not completed:
        st.info("No completed experiments yet. A research result cannot be narrated before one exists.")
        return
    selected = st.selectbox(
        "Completed experiment to summarize",
        [run["id"] for run in completed],
        key="lab_director_run",
    )
    try:
        summary = build_lab_summary(repository, project_id, selected)
    except (ValueError, KeyError, RuntimeError) as error:
        st.error(str(error))
        return
    st.text_area("Review the exact narration text", summary["text"], height=240, disabled=True, key=f"lab_director_text_{summary['fingerprint']}")
    st.download_button("Download summary", summary["text"], file_name=f"{selected}-lab-summary.md", mime="text/markdown")
    with st.expander("Summary provenance"):
        st.json(summary)
    settings = None
    try:
        settings = ElevenLabsSettings.from_environment()
    except ValueError as error:
        st.caption(f"Audio is optional. {error}")
    consent = st.checkbox(
        "Send this reviewed text to ElevenLabs to generate audio; provider usage charges may apply.",
        key=f"lab_director_audio_consent_{summary['fingerprint']}",
        disabled=settings is None,
    )
    if st.button("Hear lab summary", disabled=settings is None or not consent, key="lab_director_generate"):
        try:
            with st.spinner("Generating only the reviewed summary…"):
                create_narration_audio(
                    ElevenLabsClient(settings), repository, project_id, selected,
                    summary["fingerprint"], project_root,
                )
            st.rerun()
        except (ValueError, KeyError, RuntimeError, OSError) as error:
            st.error(str(error))
    for manifest in repository.list_narration_artifacts(project_id, selected):
        with st.expander(f"Saved audio · {manifest['summary']['captured_at']}", expanded=True):
            captured = manifest["summary"]
            if captured["fingerprint"] != summary["fingerprint"]:
                st.warning("Historical audio: research state has changed since this summary was captured. It is not current authorization.")
            try:
                audio = load_narration_audio(manifest, project_root)
            except (ValueError, KeyError, OSError) as error:
                st.error(str(error))
                continue
            st.audio(audio, format="audio/mpeg")
            st.download_button("Download MP3", audio, file_name=f"{manifest['id']}.mp3", mime="audio/mpeg", key=f"download_audio_{manifest['id']}")
            st.write(captured["text"])
            st.caption(f"Voice: {manifest['voice_id']} · Model: {manifest['model_id']} · SHA-256: {manifest['audio_sha256']}")
