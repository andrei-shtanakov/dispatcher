"""Dispatcher configuration and project auto-discovery."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dispatcher.core.collectors.base import Collector

DEFAULT_PORT = 8787
_DEFAULT_MAESTRO_DB = Path.home() / ".maestro" / "maestro.db"


def _monorepo_fallback_root() -> Path:
    """Parent of the dispatcher project — monorepo-layout convenience only.

    Standalone installs must list roots explicitly in dispatcher.toml.
    """
    return Path(__file__).resolve().parents[2].parent


@dataclass(frozen=True)
class DispatcherConfig:
    """Runtime configuration (dispatcher.toml)."""

    roots: tuple[Path, ...]
    maestro_db: Path = field(default_factory=lambda: _DEFAULT_MAESTRO_DB)
    port: int = DEFAULT_PORT
    # Empty tuple → derived from roots (prograph-vault/authored/roadmaps).
    roadmap_dirs: tuple[Path, ...] = ()
    # None → sync auto-discovery off (tests/embedding); load_config always sets it.
    tracking_file: Path | None = None
    # Optional ABSOLUTE path to the claude binary for config suggestions
    # (DESIGN-902). Distinct from spec_runner.claude_command in project.yaml
    # (that configures spec-runner; this configures dispatcher itself).
    suggest_claude_cli: Path | None = None


@dataclass(frozen=True)
class DiscoveredProject:
    """A detected project and the collector that owns it."""

    name: str
    path: Path
    collector: Collector


def load_config(config_path: Path | None = None) -> DispatcherConfig:
    """Load dispatcher.toml; absent file yields defaults."""
    data: dict = {}
    path = config_path or Path("dispatcher.toml")
    if path.is_file():
        data = tomllib.loads(path.read_text())
    roots = tuple(Path(p).expanduser() for p in data.get("roots", []))
    if not roots:
        roots = (_monorepo_fallback_root(),)
    maestro_db = Path(data.get("maestro_db", str(_DEFAULT_MAESTRO_DB))).expanduser()
    roadmap_dirs = tuple(Path(p).expanduser() for p in data.get("roadmap_dirs", []))
    tracking_file = Path(
        data.get("tracking_file", str(path.parent / "dispatcher-sync.toml"))
    ).expanduser()
    raw_suggest = data.get("suggest_claude_cli")
    suggest_claude_cli = Path(raw_suggest).expanduser() if raw_suggest else None
    return DispatcherConfig(
        roots=roots,
        maestro_db=maestro_db,
        port=int(data.get("port", DEFAULT_PORT)),
        roadmap_dirs=roadmap_dirs,
        tracking_file=tracking_file,
        suggest_claude_cli=suggest_claude_cli,
    )


def discover(
    roots: tuple[Path, ...], collectors: list[Collector]
) -> tuple[list[DiscoveredProject], list[str]]:
    """Scan roots; first match per collector wins across all roots."""
    found: list[DiscoveredProject] = []
    warnings: list[str] = []
    matched: set[str] = set()
    for root in roots:
        if not root.is_dir():
            warnings.append(f"root not found: {root}")
            continue
        try:
            # `_cowork_output` is dev-only per monorepo rules and must never
            # be read; skip it and any other hidden/underscore-prefixed dir.
            children = sorted(
                d
                for d in root.iterdir()
                if d.is_dir() and not d.name.startswith(("_", "."))
            )
        except OSError as err:
            warnings.append(f"cannot list {root}: {err}")
            continue
        for candidate in [root, *children]:
            for collector in collectors:
                if collector.name in matched:
                    continue
                try:
                    hit = collector.detect(candidate)
                except OSError:
                    continue
                if hit:
                    matched.add(collector.name)
                    found.append(
                        DiscoveredProject(
                            name=collector.name,
                            path=candidate,
                            collector=collector,
                        )
                    )
    return found, warnings
