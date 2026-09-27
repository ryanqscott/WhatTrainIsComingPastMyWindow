"""Helpers for the data files downloaded from the Rail Data Marketplace (backend/data)."""
import gzip
from pathlib import Path
from typing import IO

BACKEND_DIR = Path(__file__).resolve().parent


def resolve(path: str | None) -> Path | None:
    """Resolve a path from .env: relative paths are relative to backend/, and since
    browsers often decompress .gz downloads, 'x.json.gz' and 'x.json' are interchangeable."""
    if not path:
        return None
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = BACKEND_DIR / resolved
    if not resolved.exists():
        if resolved.suffix == ".gz":
            alternative = resolved.with_suffix("")
        else:
            alternative = resolved.with_name(resolved.name + ".gz")
        if alternative.exists():
            return alternative
    return resolved


def open_binary(path: Path) -> IO[bytes]:
    """Open a file that may or may not be gzipped."""
    with open(path, "rb") as f:
        gzipped = f.read(2) == b"\x1f\x8b"
    return gzip.open(path, "rb") if gzipped else open(path, "rb")
