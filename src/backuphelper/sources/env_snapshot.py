"""Env-snapshot source — capture a whitelist of environment variables.

Captures only explicitly whitelisted vars (exact names or fnmatch globs) into a
deterministic ``env.json``. App-specific safety (e.g. an ENCRYPTION_KEY
cross-check on restore) is left to a repo lifecycle hook, not this source.
"""

from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional

from pydantic import BaseModel, Field

from .base import Source, StagedComponent


class EnvSnapshotConfig(BaseModel):
    name: str = "env"
    whitelist: list[str] = Field(default_factory=list)


class EnvSnapshotSource(Source):
    type = "env"

    def __init__(self, spec: Mapping[str, Any], environ: Optional[Mapping[str, str]] = None):
        super().__init__(spec)
        self.cfg = EnvSnapshotConfig.model_validate({k: v for k, v in spec.items() if k != "type"})
        self._environ = dict(os.environ if environ is None else environ)

    @property
    def component_name(self) -> str:
        return self.cfg.name

    def produce(self, staging_dir: Path) -> list[StagedComponent]:
        staging_dir.mkdir(parents=True, exist_ok=True)
        captured = {
            key: value
            for key, value in self._environ.items()
            if any(fnmatch.fnmatchcase(key, pat) for pat in self.cfg.whitelist)
        }
        out = staging_dir / f"{self.cfg.name}.json"
        out.write_text(json.dumps(captured, indent=2, sort_keys=True), encoding="utf-8")
        return [StagedComponent(name=self.cfg.name, kind=self.type, path=out,
                                metadata={"var_count": len(captured)})]
