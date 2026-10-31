"""The Google pack's settings as meeseek would hand them over."""

import json

SETTINGS = {"folder_ids": ["folder"], "extra_file_ids": [], "exclude_file_ids": [], "meetings_folder_id": ""}


def use(monkeypatch, **over):
    monkeypatch.setenv("MEESEEK_PACK_CONFIG", json.dumps({**SETTINGS, **over}))
