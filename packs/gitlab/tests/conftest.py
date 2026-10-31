"""Every test runs with the test repos as the pack's settings."""

import pytest

from gitlab_test_settings import use


@pytest.fixture(autouse=True)
def _settings(monkeypatch, tmp_path):
    use(monkeypatch)
    monkeypatch.setenv("MEESEEK_PACK_DATA", str(tmp_path / "pack-data"))
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
