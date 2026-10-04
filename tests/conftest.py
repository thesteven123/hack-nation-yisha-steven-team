from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest


@pytest.fixture(autouse=True)
def isolated_connection_settings(tmp_path, monkeypatch):
    monkeypatch.setattr("scientesis.services.integration_settings.SETTINGS_PATH", tmp_path / "integration-settings.json")
