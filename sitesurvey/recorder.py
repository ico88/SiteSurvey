"""Scrittura delle sessioni di misura su disco (CSV + meta.json), robusta a cali di tensione."""
from __future__ import annotations

import csv
import json
import os
import time
from pathlib import Path

from .models import SAMPLE_FIELDS, Sample


class SessionRecorder:
    def __init__(self, data_dir: str, meta: dict, fsync_every: int = 10):
        self.started = time.time()
        self.session_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.started))
        self.dir = Path(data_dir) / "sessions" / self.session_id
        n = 1
        while self.dir.exists():
            n += 1
            self.dir = Path(data_dir) / "sessions" / f"{self.session_id}-{n}"
        self.dir.mkdir(parents=True)
        self.fsync_every = max(1, fsync_every)
        self.count = 0
        self.meta = dict(meta, session_id=self.dir.name, started=self.started)
        self._write_meta()
        self._f = (self.dir / "samples.csv").open("w", newline="", buffering=1)
        self._w = csv.writer(self._f)
        self._w.writerow(SAMPLE_FIELDS)
        self._sync()

    def _write_meta(self) -> None:
        tmp = self.dir / "meta.json.tmp"
        tmp.write_text(json.dumps(self.meta, indent=2))
        os.replace(tmp, self.dir / "meta.json")

    def _sync(self) -> None:
        self._f.flush()
        os.fsync(self._f.fileno())

    def write(self, s: Sample) -> None:
        self._w.writerow(s.row())
        self.count += 1
        if self.count % self.fsync_every == 0:
            self._sync()

    def close(self) -> None:
        if self._f.closed:
            return
        self._sync()
        self._f.close()
        self.meta.update(stopped=time.time(), samples=self.count)
        self._write_meta()
