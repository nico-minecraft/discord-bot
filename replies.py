"""Load prompt/response rows from replies.csv."""

from __future__ import annotations

import csv
import logging
from pathlib import Path

log = logging.getLogger("device-status")

REPLIES_PATH = Path(__file__).resolve().parent / "replies.csv"


class ReplyTable:
    def __init__(self, path: Path = REPLIES_PATH) -> None:
        self.path = path
        self._mtime: float | None = None
        self._rows: list[tuple[str, str]] = []

    def lookup(self, text: str) -> str | None:
        self._reload_if_changed()
        needle = text.strip().casefold()
        if not needle:
            return None
        for prompt, response in self._rows:
            if prompt == needle:
                return response
        return None

    def _reload_if_changed(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            if self._rows:
                log.warning("Could not read %s; keeping last loaded replies", self.path)
            return
        if self._mtime == mtime:
            return
        rows: list[tuple[str, str]] = []
        with self.path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "prompt" not in reader.fieldnames or "response" not in reader.fieldnames:
                log.error("%s must have prompt and response columns", self.path)
                return
            for row in reader:
                prompt = (row.get("prompt") or "").strip().casefold()
                response = (row.get("response") or "").strip()
                if not prompt or not response:
                    continue
                rows.append((prompt, response))
        self._mtime = mtime
        self._rows = rows
        log.info("Loaded %s reply rows from %s", len(rows), self.path.name)
