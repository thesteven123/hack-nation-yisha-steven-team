from __future__ import annotations

import streamlit as st

from scientesis.services.integration_settings import INTEGRATIONS, SettingsError, SettingsStore

TOKEN_ACTIONS = ("Keep current", "Replace", "Disable", "Use environment")


def render_settings_tab() -> None:
    for key in st.session_state.pop("connection_settings_clear_widgets", []):
        st.session_state.pop(key, None)
    message = st.session_state.pop("connection_settings_message", None)
    st.subheader("Connections & API settings")
    st.caption("Changes apply to new requests immediately. Saving settings never calls a provider, approves work, or starts training.")
    st.warning(
        "These are installation-wide settings, not separate user accounts. Anyone with access to this dashboard can change them. "
        "Keep this app private or behind trusted access controls."
    )
    st.info(
        "Tokens are hidden and never prefilled. Saved settings override server environment variables and persist across restarts. "
        "They are stored unencrypted in a local file excluded from Git, with owner-only permissions on POSIX systems. "
        "This is not an encrypted password vault."
    )
    if message:
        st.success(message)
    store = SettingsStore()
    try:
        store.load()
    except SettingsError as error:
        st.error(str(error))
        confirm = st.checkbox("Discard the unreadable local settings and fall back to environment/defaults.")
        if st.button("Reset local settings file", disabled=not confirm):
            try:
                store.reset_all()
                st.session_state["connection_settings_message"] = "Local settings reset. Server environment/defaults are now used."
                st.rerun()
            except SettingsError as reset_error:
                st.error(str(reset_error))
        return
    for integration in INTEGRATIONS:
        with st.expander(integration.label, expanded=True):
            st.caption(integration.note)
            if any(field.kind == "endpoint" for field in integration.fields):
                st.caption("Only use endpoints you trust: this service's token and requests will be sent there. Remote URLs require HTTPS; loopback HTTP is allowed.")
            keys = []
            values = {}
            actions = {}
            with st.form(f"connection_settings_{integration.name}"):
                for field in integration.fields:
                    key = f"connection_settings_{field.name}"
                    keys.append(key)
                    try:
                        current = store.get(field.name)
                        status = store.status(field.name)
                    except SettingsError as error:
                        st.warning(f"{field.label}: {error}")
                        current = field.default
                        status = {"configured": False, "source": "invalid server setting", "disabled": False}
                    if field.secret:
                        label = "Disabled locally" if status["disabled"] else "Configured" if status["configured"] else "Not configured"
                        st.caption(f"{field.label}: {label} · {status['source']}")
                        action_key = f"{key}_action"
                        keys.append(action_key)
                        actions[field.name] = st.selectbox(f"{field.label} action", TOKEN_ACTIONS, key=action_key)
                        values[field.name] = st.text_input(f"New {field.label.lower()}", type="password", key=key, max_chars=field.limit)
                    else:
                        values[field.name] = st.text_input(field.label, value=current, key=key, max_chars=field.limit)
                st.caption("Blank token + Keep current leaves it unchanged. Replace requires a new token. Disable blocks environment fallback. Use environment removes the saved override.")
                save = st.form_submit_button(f"Save {integration.label} settings")
                restore = st.form_submit_button(f"Restore {integration.label} environment/defaults")
            if save or restore:
                try:
                    changes = {}
                    reset = set()
                    if restore:
                        reset = {field.name for field in integration.fields}
                    else:
                        for field in integration.fields:
                            value = values[field.name]
                            if not field.secret:
                                changes[field.name] = value
                            elif actions[field.name] == "Replace":
                                if not value.strip():
                                    raise SettingsError("Enter a new token before choosing Replace. The current token was not changed.")
                                changes[field.name] = value
                            elif actions[field.name] == "Disable":
                                changes[field.name] = ""
                            elif actions[field.name] == "Use environment":
                                reset.add(field.name)
                            elif value:
                                raise SettingsError("Choose Replace to save the new token, or clear the new-token field to keep the current token.")
                    store.save(changes, reset)
                    st.session_state["connection_settings_clear_widgets"] = keys
                    st.session_state["connection_settings_message"] = (
                        f"{integration.label} restored to server environment/defaults."
                        if restore else f"{integration.label} settings saved. New requests use them immediately."
                    )
                    st.rerun()
                except SettingsError as error:
                    st.error(str(error))
