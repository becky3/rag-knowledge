"""`python -m rag.server` のエントリポイント."""

from __future__ import annotations

from .bootstrap import _configure_and_run

if __name__ == "__main__":
    _configure_and_run()
