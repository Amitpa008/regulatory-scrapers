from __future__ import annotations

import hashlib
from pathlib import Path


class PDFStore:
    def __init__(self, root_dir: str | Path = "data/pdfs") -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def sha256_bytes(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def save(self, source: str, content: bytes, suffix: str = ".pdf") -> tuple[Path, str]:
        digest = self.sha256_bytes(content)
        source_dir = self.root_dir / source
        source_dir.mkdir(parents=True, exist_ok=True)
        path = source_dir / f"{digest}{suffix}"
        if not path.exists():
            path.write_bytes(content)
        return path, digest

