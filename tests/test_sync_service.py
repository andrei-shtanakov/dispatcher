"""TASK-203: render path never awaits the network run; freshness metadata."""

import subprocess
import threading
import time
from pathlib import Path

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.sync import SyncReport
from dispatcher.core.sync_service import SyncService, fetch_workspace


def make_report() -> SyncReport:
    return SyncReport(current_host="mac-a", top_line="ok")


def make_service(tmp_path: Path, fetcher, collector=None):
    calls = {"collect": 0}

    def counting_collector(config: DispatcherConfig) -> SyncReport:
        calls["collect"] += 1
        return make_report()

    service = SyncService(
        DispatcherConfig(roots=(tmp_path,)),
        collector=collector or counting_collector,
        fetcher=fetcher,
    )
    return service, calls


def test_get_never_awaits_fetch(tmp_path: Path) -> None:
    release = threading.Event()
    started = threading.Event()

    def blocking_fetcher(workspace: Path) -> list[str]:
        started.set()
        release.wait(timeout=10)
        return []

    service, _ = make_service(tmp_path, blocking_fetcher)
    t0 = time.monotonic()
    status = service.get()
    elapsed = time.monotonic() - t0

    assert elapsed < 0.5, "render path awaited the network run"
    assert started.wait(timeout=2)
    assert status.fetch_in_flight
    assert status.last_fetch_at is None
    release.set()


def test_fetch_completion_invalidates_report_cache(tmp_path: Path) -> None:
    done = threading.Event()

    def instant_fetcher(workspace: Path) -> list[str]:
        done.set()
        return []

    service, calls = make_service(tmp_path, instant_fetcher)
    service.get()
    assert done.wait(timeout=2)
    assert service.wait_for_fetch(2)

    status = service.get(start_fetch=False)
    assert not status.fetch_in_flight
    assert status.last_fetch_at is not None
    assert status.last_fetch_error is None
    # завершившийся fetch сбросил TTL-кэш → отчёт пересобран с новыми refs
    assert calls["collect"] == 2


def test_report_is_cached_within_ttl(tmp_path: Path) -> None:
    service, calls = make_service(tmp_path, lambda ws: [])
    service.get(start_fetch=False)
    service.get(start_fetch=False)
    assert calls["collect"] == 1


def test_fetch_not_restarted_within_min_interval(tmp_path: Path) -> None:
    fetches = {"n": 0}

    def counting_fetcher(workspace: Path) -> list[str]:
        fetches["n"] += 1
        return []

    service, _ = make_service(tmp_path, counting_fetcher)
    service.get()
    assert service.wait_for_fetch(2)
    service.get()  # внутри min-interval — второй прогон не стартует
    assert fetches["n"] == 1


def test_crashing_fetcher_surfaces_error_and_service_survives(tmp_path: Path) -> None:
    def crashing_fetcher(workspace: Path) -> list[str]:
        raise OSError("subprocess exploded")

    service, _ = make_service(tmp_path, crashing_fetcher)
    service.get()
    assert service.wait_for_fetch(2)
    status = service.get(start_fetch=False)
    assert not status.fetch_in_flight
    assert "fetch run crashed" in (status.last_fetch_error or "")


def test_fetch_errors_surface_in_status(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, lambda ws: ["alpha: fetch failed"])
    service.get()
    assert service.wait_for_fetch(2)
    status = service.get(start_fetch=False)
    assert status.last_fetch_error == "alpha: fetch failed"


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    )


def test_fetch_workspace_updates_refs_and_reports_failures(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "main")
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    (seed / "f.txt").write_text("one\n")
    _git(seed, "add", "f.txt")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "-u", "origin", "main")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(workspace / "alpha")],
        check=True,
        capture_output=True,
    )
    broken = workspace / "broken"
    broken.mkdir()
    _git(broken, "init", "-q")
    _git(broken, "remote", "add", "origin", str(tmp_path / "nope.git"))
    hidden = workspace / "_scratch"
    hidden.mkdir()
    _git(hidden, "init", "-q")
    _git(hidden, "remote", "add", "origin", str(tmp_path / "nope.git"))

    # новый коммит в origin: fetch должен подтянуть remote-tracking ref
    (seed / "f.txt").write_text("two\n")
    _git(seed, "commit", "-qam", "update")
    _git(seed, "push", "-q")

    errors = fetch_workspace(workspace)

    assert any(e.startswith("broken:") for e in errors)
    assert not any("_scratch" in e for e in errors), "underscore-dir не пропущен"
    counts = subprocess.run(
        [
            "git",
            "-C",
            str(workspace / "alpha"),
            "rev-list",
            "--count",
            "HEAD..@{upstream}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert counts == "1", "fetch не обновил remote-tracking refs"
