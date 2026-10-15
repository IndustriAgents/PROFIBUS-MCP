from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from profibus_mcp.domain.model import alias_problem


class SlaveMap:
    """Alias -> slave map from ``SLAVE_MAP_FILE``, reloaded when the file changes."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._slaves: dict[str, Any] = {}
        self._stamp: tuple[int, int] | None = None
        self.error: str | None = None
        self.refresh()

    def refresh(self) -> None:
        if not self.path:
            self._slaves, self._stamp, self.error = ({}, None, None)
            return
        try:
            stat = self.path.stat()
        except OSError as exc:
            self._slaves, self._stamp = ({}, None)
            self.error = f"SLAVE_MAP_FILE {self.path} cannot be read: {exc.strerror}"
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return
        self._stamp = stamp
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            self._slaves = {}
            self.error = f"SLAVE_MAP_FILE {self.path} is not valid JSON: {exc}"
            return
        if not isinstance(data, dict):
            self._slaves = {}
            self.error = f"SLAVE_MAP_FILE {self.path} must hold a JSON object of alias -> slave"
            return
        self._slaves = {str(alias): spec for alias, spec in data.items()}
        self.error = None

    def get(self, alias: str) -> Any:
        self.refresh()
        return self._slaves.get(alias)

    def entries(self) -> list[dict[str, Any]]:
        self.refresh()
        listed = []
        for alias, spec in self._slaves.items():
            entry: dict[str, Any] = {"alias": alias}
            if isinstance(spec, dict):
                entry["address"] = spec.get("address")
                entry["description"] = spec.get("description")
            problem = alias_problem(spec)
            if problem:
                entry["error"] = problem
            listed.append(entry)
        return listed

    def count(self) -> int:
        self.refresh()
        return len(self._slaves)
