"""Files that come with a message (images, PDFs), kept by their content.

A message's event names each by its hash; the bytes live once, under the
home's .cache/blobs/, however many turns show them. Read back to show a
reopened conversation, and to send the model what it was shown.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


class FileBlobs:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def put(self, data: bytes) -> str:
        ref = hashlib.sha256(data).hexdigest()[:32]
        path = self.root / ref[:2] / ref
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return ref

    def get(self, ref: str) -> bytes:
        return (self.root / ref[:2] / ref).read_bytes()


def blobs() -> FileBlobs:
    from meeseek.config.paths import home

    return FileBlobs(home().data / "blobs")
