"""Load/save :class:`AppSettings` as JSON (atomic writes)."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from ssh_terminal.models.app_settings import AppSettings
from ssh_terminal.utils.paths import get_app_config_path

log = logging.getLogger(__name__)


class SettingsService:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or get_app_config_path()
        self.settings = AppSettings()
        self._listeners: list[Callable[[AppSettings], None]] = []

    def load(self) -> AppSettings:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("root is not an object")
                self.settings = AppSettings.from_dict(data)
                log.info("App settings loaded from %s", self.path)
            except (OSError, ValueError) as exc:
                broken = self.path.with_suffix(".json.broken")
                log.error("Invalid app settings (%s); moved to %s and using defaults", exc, broken)
                try:
                    self.path.replace(broken)
                except OSError as move_exc:
                    log.error("Could not move broken settings: %s", move_exc)
                self.settings = AppSettings()
        else:
            self.settings = AppSettings()
        return self.settings

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.settings.to_dict(), indent=4, ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".app_config.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        for listener in list(self._listeners):
            listener(self.settings)

    def subscribe(self, listener: Callable[[AppSettings], None]) -> None:
        self._listeners.append(listener)
