"""Every test runs with the two example environments as the pack's settings."""

import pytest

from gcp_test_settings import use


@pytest.fixture(autouse=True)
def _settings(monkeypatch, tmp_path):
    use(monkeypatch)
    monkeypatch.setenv("MEESEEK_PACK_DATA", str(tmp_path / "pack-data"))
