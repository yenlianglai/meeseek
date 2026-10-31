"""Every test runs with the example settings as the pack's."""

import pytest

from google_test_settings import use


@pytest.fixture(autouse=True)
def _settings(monkeypatch, tmp_path):
    use(monkeypatch)
    monkeypatch.setenv("MEESEEK_PACK_DATA", str(tmp_path / "pack-data"))
