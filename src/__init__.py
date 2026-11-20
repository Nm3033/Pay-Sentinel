"""Pay Sentinel business logic."""
from pathlib import Path


def source_stamp() -> int:
    """Newest modification time of the src/*.py files (lets app.py notice code updated after a deploy)."""
    return max(p.stat().st_mtime_ns for p in Path(__file__).resolve().parent.glob("*.py"))


_loaded_stamp = source_stamp()   # the code version this process imported
