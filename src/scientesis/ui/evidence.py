from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import streamlit as st

from scientesis.adapters.firecrawl import FirecrawlClient, FirecrawlError, ScrapedPage, UnsafeSourceURL
from scientesis.services.evidence import load_evidence_content, save_evidence_candidate

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CURATED_SOURCES = {
    "Gymnasium Reacher-v5 documentation": ("https://gymnasium.farama.org/environments/mujoco/reacher/", "official_docs"),
    "Stable-Baselines3 PPO documentation": ("https://stable-baselines3.readthedocs.io/en/master/modules/ppo.html", "official_docs"),
    "Stable-Baselines3 reinforcement-learning tips": ("https://stable-baselines3.readthedocs.io/en/master/guide/rl_tips.html", "official_docs"),
    "Custom public source": ("", "technical_blog"),
}
SOURCE_TYPES = {
    "Official documentation": "official_docs",
    "Peer-reviewed paper": "peer_reviewed",
    "Preprint": "preprint",
    "Technical blog": "technical_blog",
}


def render_evidence_tab(repository, project_id: str) -> None:
    st.subheader("Curated external evidence")
    st.caption(
        "Fetch public, unauthenticated pages only. Firecrawl receives the URL; never use this for private, "
        "login-gated, or paywalled content. A captured source remains pending until you review it."
    )
    st.info(
        "External sources are method-design context only. They cannot count as this lab's experiment results "
        "or establish safety of a physical robot."
    )

    with st.form("firecrawl_fetch_form"):
        source_choice = st.selectbox("Curated source", list(CURATED_SOURCES))
        suggested_url, suggested_type = CURATED_SOURCES[source_choice]
        source_url = st.text_input("Public source URL", value=suggested_url)
        fetch = st.form_submit_button("Fetch page for review")
    if fetch:
        try:
            page = FirecrawlClient().scrape(source_url)
            st.session_state["scientesis_scraped_evidence"] = {
                "page": asdict(page),
                "suggested_type": suggested_type,
            }
            st.success(f"Fetched {page.title}. Review the excerpt and complete the evidence card below.")
        except (FirecrawlError, UnsafeSourceURL, ValueError) as error:
            st.error(str(error))

    scraped = st.session_state.get("scientesis_scraped_evidence")
    if scraped:
        page = ScrapedPage(**scraped["page"])
        st.markdown(f"**Fetched page:** {page.title}")
        st.code(page.markdown[:4_000], language=None)
        if len(page.markdown) > 4_000:
            st.caption("Excerpt shown; the complete fetched text will be preserved unchanged.")
        default_type = scraped.get("suggested_type", "technical_blog")
        with st.form("evidence_card_form"):
            title = st.text_input("Evidence title", value=page.title)
            source_type_label = st.selectbox(
                "Evidence level",
                list(SOURCE_TYPES),
                index=list(SOURCE_TYPES.values()).index(default_type) if default_type in SOURCE_TYPES.values() else 3,
            )
            relevance = st.selectbox("Relevance to this project", ["high", "medium", "low"], index=1)
            claim = st.text_area("What claim does the source make?", max_chars=4_000)
            scope = st.text_area("What setting or population does it cover?", max_chars=4_000)
            limitations = st.text_area("What does it not establish?", max_chars=4_000)
            implementation_hint = st.text_area("How might it inform this lab's method?", max_chars=2_000)
            save = st.form_submit_button("Save as pending evidence card")
        if save:
            details = {
                "title": title,
                "evidence_level": SOURCE_TYPES[source_type_label],
                "relevance": relevance,
                "claim": claim,
                "scope": scope,
                "limitations": limitations,
                "implementation_hint": implementation_hint,
            }
            try:
                card = save_evidence_candidate(repository, project_id, details, page, PROJECT_ROOT)
                st.session_state.pop("scientesis_scraped_evidence", None)
                st.success(f"Saved {card['id']} for human review. It is not yet approved for use.")
                st.rerun()
            except (ValueError, OSError) as error:
                st.error(str(error))

    st.divider()
    st.subheader("Source review queue")
    cards = repository.list_evidence_cards(project_id)
    if not cards:
        st.caption("No evidence cards yet. Fetch one of the curated sources above to begin.")
    for card in cards:
        is_pending = card["approval_status"] == "pending_review"
        status_label = "Pending review" if is_pending else card["approval_status"].replace("_", " ").title()
        with st.container(border=True):
            st.markdown(f"**{card['id']} · {status_label} · {card['quality'].get('evidence_level', card['source_type'])}**")
            st.write(card["title"])
            st.text(card["source_url"])
            st.markdown("**External claim**")
            st.text(card["claim_text"])
            st.markdown("**Scope**")
            st.text(card["scope_text"])
            st.markdown("**Limitations**")
            st.text(card["limitations_text"])
            st.markdown("**Potential lab use**")
            st.text(card["implementation_hint"])
            st.caption(
                f"Relevance: {card['quality'].get('relevance', 'unrated')} · "
                f"Use: {card['quality'].get('approved_for', 'method_design_only')} · "
                f"Claim status: {card['quality'].get('claim_status', 'external_prior_not_local_result')}"
            )
            with st.expander("Preserved source text"):
                try:
                    st.text(load_evidence_content(card, PROJECT_ROOT)[:20_000])
                except ValueError as error:
                    st.error(str(error))
            if is_pending:
                left, right = st.columns(2)
                if left.button("Approve for method design", key=f"approve-evidence-{card['id']}"):
                    repository.review_evidence_card(card["id"], "approved")
                    st.rerun()
                if right.button("Reject source", key=f"reject-evidence-{card['id']}"):
                    repository.review_evidence_card(card["id"], "rejected")
                    st.rerun()
