"""Collecting Shijhon's log lines in tests (the app runs in the test process)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager


class Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@contextmanager
def collected(name: str) -> Iterator[list[str]]:
    """The INFO (and higher) lines of logger ``name`` while the block runs."""
    collector = Collect()
    logger = logging.getLogger(name)
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(collector)
    try:
        yield collector.lines
    finally:
        logger.removeHandler(collector)
        logger.setLevel(previous)
