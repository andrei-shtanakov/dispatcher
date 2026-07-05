"""Tests for the shared SnapshotService and error-freshness helpers."""

import re
import threading
import time
from pathlib import Path

from conftest import make_arbiter, make_maestro_home

from dispatcher.core import service as service_mod
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.service import ERRORS_DAYS_DEFAULT, SnapshotService


def _config(tmp_path: Path) -> DispatcherConfig:
    make_arbiter(tmp_path)
    db = make_maestro_home(tmp_path)
    return DispatcherConfig(roots=(tmp_path,), maestro_db=db)


def test_collects_detected_and_undetected(tmp_path: Path) -> None:
    snapshots, _ = SnapshotService(_config(tmp_path)).get()
    by_name = {s.name: s for s in snapshots}
    assert by_name["arbiter"].detected is True
    assert by_name["atp-platform"].detected is False
    assert len(snapshots) == 5  # every registered collector gets a row


def test_ttl_cache_returns_same_object(tmp_path: Path) -> None:
    svc = SnapshotService(_config(tmp_path))
    assert svc.get() is svc.get()


def test_collector_crash_degrades_to_warning(tmp_path: Path, monkeypatch) -> None:
    from dispatcher.core.collectors import COLLECTORS

    arbiter = next(c for c in COLLECTORS if c.name == "arbiter")

    def boom(self, path, ctx):  # noqa: ANN001, ARG001
        raise RuntimeError("kaput")

    monkeypatch.setattr(type(arbiter), "collect", boom)
    snapshots, _ = SnapshotService(_config(tmp_path)).get()
    snap = next(s for s in snapshots if s.name == "arbiter")
    assert any("collector crashed" in w for w in snap.warnings)


def test_concurrent_get_collects_once(tmp_path: Path, monkeypatch) -> None:
    svc = SnapshotService(_config(tmp_path))
    calls: list[int] = []
    real_collect = svc._collect

    def slow_collect():
        calls.append(1)
        time.sleep(0.05)
        return real_collect()

    monkeypatch.setattr(svc, "_collect", slow_collect)
    threads = [threading.Thread(target=svc.get) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1  # losers of the race are served from the cache


def test_web_days_default_matches_core() -> None:
    # The web JS cannot import the core constant and keeps its own copy.
    html_path = (
        Path(service_mod.__file__).parents[1] / "server" / "static" / "index.html"
    )
    match = re.search(r"ERRORS_DAYS_DEFAULT = (\d+)", html_path.read_text())
    assert match is not None
    assert int(match.group(1)) == ERRORS_DAYS_DEFAULT
