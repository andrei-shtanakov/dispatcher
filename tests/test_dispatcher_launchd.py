"""The launchd installer keeps its three generated agents coherent."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "dispatcher_launchd.sh"


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, str], Path, Path]:
    """A standalone checkout plus harmless launchctl/lsof/uv substitutes."""
    checkout = tmp_path / "dispatcher"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SCRIPT, scripts / SCRIPT.name)
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)

    home = tmp_path / "home"
    config = home / ".config/dispatcher/dispatcher.toml"
    config.parent.mkdir(parents=True)
    config.write_text("port = 18787\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "launchctl.calls"
    (fake_bin / "launchctl").write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$LAUNCHCTL_CALLS"\n',
        encoding="utf-8",
    )
    (fake_bin / "lsof").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (fake_bin / "uv").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for command in fake_bin.iterdir():
        command.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "LAUNCHCTL_CALLS": str(calls),
    }
    return checkout, env, config, calls


def _install(
    checkout: Path, env: dict[str, str], config: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/dispatcher_launchd.sh", "install", "--config", str(config)],
        cwd=checkout,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


def test_snapshot_agent_uses_the_services_resolved_config(tmp_path: Path) -> None:
    checkout, env, config, _ = _fixture(tmp_path)
    checker = Path(env["HOME"]) / ".local/share/dispatcher-pinned-checker/bin"
    checker.mkdir(parents=True)
    (checker / "github-checker").write_text("#!/bin/sh\n", encoding="utf-8")
    (checker / "github-checker").chmod(0o755)

    _install(checkout, env, config)

    plist = Path(env["HOME"]) / "Library/LaunchAgents/dev.atp.dispatcher.snapshot.plist"
    with plist.open("rb") as stream:
        args = plistlib.load(stream)["ProgramArguments"]
    assert args[-2:] == ["--config", str(config.resolve())]


def test_reinstall_without_checker_removes_the_old_snapshot_agent(
    tmp_path: Path,
) -> None:
    checkout, env, config, calls = _fixture(tmp_path)
    checker = Path(env["HOME"]) / ".local/share/dispatcher-pinned-checker/bin"
    checker.mkdir(parents=True)
    binary = checker / "github-checker"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    _install(checkout, env, config)

    binary.unlink()
    calls.write_text("", encoding="utf-8")
    result = _install(checkout, env, config)

    plist = Path(env["HOME"]) / "Library/LaunchAgents/dev.atp.dispatcher.snapshot.plist"
    assert not plist.exists()
    assert "SKIPPED dev.atp.dispatcher.snapshot" in result.stderr
    assert "bootout gui/" in calls.read_text(encoding="utf-8")
    assert "/dev.atp.dispatcher.snapshot" in calls.read_text(encoding="utf-8")


def test_rotate_covers_every_log_in_the_directory(tmp_path: Path) -> None:
    """codex on #196: the snapshot agent added a third log and the rotator
    still named only out/err — the slow-full-disk failure rotation exists
    to stop, reintroduced by the PR that added a new writer. The glob must
    rotate any *.log, including ones no one has invented yet."""
    checkout, env, config, _ = _fixture(tmp_path)
    log_dir = Path(env["HOME"]) / "Library/Logs/dispatcher"
    log_dir.mkdir(parents=True)
    big = b"x" * (10 * 1024 * 1024 + 1)
    for name in ("out.log", "snapshot.log", "future-agent.log"):
        (log_dir / name).write_bytes(big)
    (log_dir / "small.log").write_bytes(b"tiny")

    subprocess.run(
        ["bash", "scripts/dispatcher_launchd.sh", "rotate"],
        cwd=checkout,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    for name in ("out.log", "snapshot.log", "future-agent.log"):
        assert (log_dir / f"{name}.1").exists(), f"{name} was not rotated"
        assert (log_dir / name).stat().st_size == 0
    assert not (log_dir / "small.log.1").exists(), "under-threshold rotated"


def test_reinstall_retries_the_snapshot_bootstrap_transient(tmp_path: Path) -> None:
    """codex on #196: the main service got the bootstrap retry, the
    snapshot agent reused bootout/bootstrap without it — under set -e one
    transient "error 5" aborted install with the publisher unloaded. Every
    agent's bootstrap must survive one transient failure."""
    checkout, env, config, calls = _fixture(tmp_path)
    checker_bin = Path(env["HOME"]) / ".local/share/dispatcher-pinned-checker/bin"
    checker_bin.mkdir(parents=True)
    (checker_bin / "github-checker").write_text(
        "#!/bin/sh" + chr(10) + "exit 0" + chr(10)
    )
    (checker_bin / "github-checker").chmod(0o755)

    # launchctl: bootstrap of the SNAPSHOT label fails once, then works —
    # the transient the retry exists for; everything else succeeds.
    fake = Path(env["PATH"].split(":")[0]) / "launchctl"
    marker = tmp_path / "failed-once"
    script = "\n".join(
        [
            "#!/bin/sh",
            'printf "%s\\n" "$*" >> "$LAUNCHCTL_CALLS"',
            'case "$*" in',
            "  bootstrap*snapshot*)",
            f'    if [ ! -f "{marker}" ]; then touch "{marker}"; exit 5; fi ;;',
            "esac",
            "exit 0",
            "",
        ]
    )
    fake.write_text(script, encoding="utf-8")

    result = _install(checkout, env, config)  # check=True: must not abort
    assert "installed dev.atp.dispatcher.snapshot" in result.stderr
    boots = [
        c
        for c in Path(env["LAUNCHCTL_CALLS"]).read_text().splitlines()
        if c.startswith("bootstrap") and "snapshot" in c
    ]
    assert len(boots) == 2, f"expected retry after the transient, got {boots}"


def _foreign_listener(env: dict[str, str], holder_pid: str) -> None:
    """Make the stubbed lsof report a foreign process on the port."""
    fake = Path(env["PATH"].split(":")[0]) / "lsof"
    fake.write_text(f'#!/bin/sh\necho "{holder_pid}"\nexit 0\n', encoding="utf-8")
    fake.chmod(0o755)


def test_port_gate_refuses_when_our_job_is_not_loaded(tmp_path: Path) -> None:
    """review on #196: with the job absent, `launchctl print` fails and —
    under set -euo pipefail — used to abort install with NO message at all
    instead of the promised "already served" refusal. An absent own job
    plus any listener = a foreign listener, named refusal required."""
    checkout, env, config, _ = _fixture(tmp_path)
    _foreign_listener(env, "4242")
    fake = Path(env["PATH"].split(":")[0]) / "launchctl"
    fake.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$LAUNCHCTL_CALLS"\n'
        'case "$1" in print) exit 113 ;; esac\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)

    proc = subprocess.run(
        ["bash", "scripts/dispatcher_launchd.sh", "install", "--config", str(config)],
        cwd=checkout,
        env=env,
        text=True,
        capture_output=True,
    )
    assert proc.returncode != 0
    assert "already served by PID 4242" in proc.stderr


def test_port_gate_refuses_when_our_job_has_no_pid(tmp_path: Path) -> None:
    """review on #196: a loaded job with no `pid =` line yields own="";
    when the holder's parent walk also bottoms out empty, "" == "" used to
    read as "that's us" and install proceeded onto a port held by a
    FOREIGN process. A match must require a literal, non-empty PID."""
    checkout, env, config, calls = _fixture(tmp_path)
    # holder pid 1: its parent walk ends at 0/empty fast, and it is
    # certainly not our (absent) service
    _foreign_listener(env, "1")
    fake = Path(env["PATH"].split(":")[0]) / "launchctl"
    fake.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$LAUNCHCTL_CALLS"\n'
        'case "$1" in print) echo "state = running" ;; esac\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)

    proc = subprocess.run(
        ["bash", "scripts/dispatcher_launchd.sh", "install", "--config", str(config)],
        cwd=checkout,
        env=env,
        text=True,
        capture_output=True,
    )
    assert proc.returncode != 0
    assert "already served by PID 1" in proc.stderr
    # and the gate refused BEFORE any bootstrap was attempted
    logged = calls.read_text(encoding="utf-8") if calls.exists() else ""
    assert "bootstrap" not in logged


def test_main_service_path_includes_the_claude_bin_dir(tmp_path: Path) -> None:
    """Live acceptance, run 01M11…: the panel's first real launch died in
    6ms — maestro, spawned by the launchd service, inherited launchd's
    bare PATH and could not find the `claude` binary (~/.local/bin).
    The generated plist must carry an explicit PATH with the invoking
    user's ~/.local/bin ahead of the system dirs."""
    checkout, env, config, _ = _fixture(tmp_path)
    _install(checkout, env, config)
    plist = Path(env["HOME"]) / "Library/LaunchAgents/dev.atp.dispatcher.plist"
    with plist.open("rb") as stream:
        loaded = plistlib.load(stream)
    path_value = loaded["EnvironmentVariables"]["PATH"]
    assert path_value.startswith(env["HOME"] + "/.local/bin:")
    assert "/usr/local/bin" in path_value
