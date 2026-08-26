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
