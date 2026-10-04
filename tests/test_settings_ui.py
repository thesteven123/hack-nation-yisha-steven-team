from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from scientesis.adapters.llm import LLMSettings
from scientesis.db.repository import Repository
from scientesis.services.integration_settings import FIELDS, SettingsStore


@pytest.fixture
def settings_screen(monkeypatch):
    for name in FIELDS:
        monkeypatch.delenv(name, raising=False)
    screen = AppTest.from_string(
        "from scientesis.ui.settings import render_settings_tab\nrender_settings_tab()",
        default_timeout=20,
    ).run()
    assert not screen.exception
    return screen


def press(screen, label):
    next(button for button in screen.button if button.label == label).click().run()
    assert not screen.exception


def visible_messages(screen):
    return "\n".join(
        str(element.value)
        for kind in ("caption", "info", "warning", "error", "success", "markdown")
        for element in getattr(screen, kind)
    )


def test_save_change_and_clear_tokens_without_prefilling_them(settings_screen):
    screen = settings_screen
    token_key = "connection_settings_SCIENTESIS_LLM_API_KEY"
    screen.selectbox(key=f"{token_key}_action").set_value("Replace")
    screen.text_input(key=token_key).set_value("private-ui-test-token")
    screen.text_input(key="connection_settings_SCIENTESIS_LLM_MODEL").set_value("mock-model")
    screen.text_input(key="connection_settings_SCIENTESIS_LLM_BASE_URL").set_value("http://localhost:9000/v1")
    press(screen, "Save LLM synthesis settings")
    assert LLMSettings.from_environment().model == "mock-model"
    assert LLMSettings.from_environment().api_key == "private-ui-test-token"
    assert screen.text_input(key=token_key).value == ""
    assert screen.selectbox(key=f"{token_key}_action").value == "Keep current"
    assert "private-ui-test-token" not in visible_messages(screen)
    screen.text_input(key="connection_settings_SCIENTESIS_LLM_MODEL").set_value("new-model")
    press(screen, "Save LLM synthesis settings")
    assert LLMSettings.from_environment().api_key == "private-ui-test-token"
    assert LLMSettings.from_environment().model == "new-model"
    screen.selectbox(key=f"{token_key}_action").set_value("Disable")
    press(screen, "Save LLM synthesis settings")
    assert SettingsStore().get("SCIENTESIS_LLM_API_KEY") == ""
    assert "Disabled locally" in visible_messages(screen)


def test_restore_and_use_environment_do_not_display_existing_keys(settings_screen, monkeypatch):
    screen = settings_screen
    monkeypatch.setenv("FIRECRAWL_API_KEY", "server-env-private-key")
    store = SettingsStore()
    store.save({"FIRECRAWL_API_KEY": "saved-private-key"})
    screen.run()
    token_key = "connection_settings_FIRECRAWL_API_KEY"
    assert screen.text_input(key=token_key).value == ""
    screen.selectbox(key=f"{token_key}_action").set_value("Use environment")
    press(screen, "Save Firecrawl settings")
    assert store.get("FIRECRAWL_API_KEY") == "server-env-private-key"
    assert "saved-private-key" not in visible_messages(screen)
    assert "server-env-private-key" not in visible_messages(screen)
    store.save({"FIRECRAWL_API_URL": "https://firecrawl.example/v2/scrape"})
    screen.run()
    press(screen, "Restore Firecrawl environment/defaults")
    assert "FIRECRAWL_API_URL" not in store.load()


def test_blank_replace_and_untrusted_plain_http_leave_settings_unchanged(settings_screen):
    screen = settings_screen
    store = SettingsStore()
    store.save({"FIRECRAWL_API_KEY": "saved-private-key"})
    screen.run()
    token_key = "connection_settings_FIRECRAWL_API_KEY"
    screen.selectbox(key=f"{token_key}_action").set_value("Replace")
    press(screen, "Save Firecrawl settings")
    assert any("Enter a new token" in error.value for error in screen.error)
    assert store.get("FIRECRAWL_API_KEY") == "saved-private-key"
    screen.selectbox(key=f"{token_key}_action").set_value("Keep current")
    screen.text_input(key="connection_settings_FIRECRAWL_API_URL").set_value("http://external.example/v2/scrape")
    press(screen, "Save Firecrawl settings")
    assert any("HTTPS" in error.value for error in screen.error)
    assert store.load() == {"FIRECRAWL_API_KEY": "saved-private-key"}


def test_full_dashboard_settings_save_has_no_research_side_effects(tmp_path, monkeypatch):
    for name in FIELDS:
        monkeypatch.delenv(name, raising=False)
    database = tmp_path / "ui.sqlite3"
    monkeypatch.setenv("SCIENTESIS_DB_PATH", str(database))
    def deny_provider_call(*args, **kwargs):
        raise AssertionError("Saving settings must not contact a provider.")
    monkeypatch.setattr("scientesis.adapters.llm.OpenAICompatibleClient.complete_json", deny_provider_call)
    monkeypatch.setattr("scientesis.adapters.firecrawl.FirecrawlClient.scrape", deny_provider_call)
    monkeypatch.setattr("scientesis.adapters.moss.MossAdapter.sync_documents", deny_provider_call)
    monkeypatch.setattr("scientesis.adapters.elevenlabs.ElevenLabsClient.synthesize", deny_provider_call)
    root = Path(__file__).resolve().parents[1]
    screen = AppTest.from_file(str(root / "app.py"), default_timeout=30).run()
    assert not screen.exception
    assert "Settings" in [tab.label for tab in screen.tabs]
    repository = Repository(database)
    project_id = repository.get_project_id()
    before = repository.list_audit_events(project_id)
    token_key = "connection_settings_ELEVENLABS_API_KEY"
    screen.selectbox(key=f"{token_key}_action").set_value("Replace")
    screen.text_input(key=token_key).set_value("private-audio-token")
    screen.text_input(key="connection_settings_ELEVENLABS_VOICE_ID").set_value("voice123")
    press(screen, "Save ElevenLabs audio settings")
    assert not repository.list_runs(project_id)
    assert not repository.list_proposals(project_id)
    assert repository.list_audit_events(project_id) == before
    assert "private-audio-token" not in visible_messages(screen)
