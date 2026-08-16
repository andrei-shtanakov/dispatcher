"""Dispatcher configuration and project auto-discovery."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

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
    # Maestro home for per-project run enumeration (#147). None → derived
    # beside the legacy db (`maestro_db.parent`), which keeps configs that
    # only set maestro_db — and every test tree — hermetic.
    maestro_home: Path | None = None
    port: int = DEFAULT_PORT
    # Empty tuple → derived from roots (prograph-vault/authored/roadmaps).
    roadmap_dirs: tuple[Path, ...] = ()
    # None → sync auto-discovery off (tests/embedding); load_config always sets it.
    tracking_file: Path | None = None
    # Optional ABSOLUTE path to the claude binary for config suggestions
    # (DESIGN-902). Distinct from spec_runner.claude_command in project.yaml
    # (that configures spec-runner; this configures dispatcher itself).
    suggest_claude_cli: Path | None = None
    # Base URL of one ATP eco server (spec 2026-08-15 §3). None → the
    # benchmark view is off: no service, hidden panel, "unconfigured" report.
    benchmarks_url: str | None = None
    # PATH to the ATP user-token file (phase-2 spec 2026-08-16 §3) — the
    # config never carries the token itself. None → run-status is off.
    benchmarks_token_file: Path | None = None

    @property
    def effective_maestro_home(self) -> Path:
        """Home for `projects/<host>/<owner>/<repo>/runs/` enumeration."""
        if self.maestro_home is not None:
            return self.maestro_home
        return self.maestro_db.parent


@dataclass(frozen=True)
class DiscoveredProject:
    """A detected project and the collector that owns it."""

    name: str
    path: Path
    collector: Collector


def _validate_benchmarks_url(raw: object) -> str:
    """Spec §3: absolute http(s) URL, host required, no query/fragment/
    userinfo; base path allowed; trailing slash stripped."""
    if not isinstance(raw, str):
        raise ValueError("benchmarks.url must be a string")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"benchmarks.url must be http(s), got: {raw!r}")
    if not parts.hostname:
        raise ValueError(f"benchmarks.url must include a host: {raw!r}")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError(
            f"benchmarks.url must not carry query/fragment/userinfo: {raw!r}"
        )
    return raw.rstrip("/")


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
    raw_home = data.get("maestro_home")
    maestro_home = Path(raw_home).expanduser() if raw_home else None
    roadmap_dirs = tuple(Path(p).expanduser() for p in data.get("roadmap_dirs", []))
    tracking_file = Path(
        data.get("tracking_file", str(path.parent / "dispatcher-sync.toml"))
    ).expanduser()
    raw_suggest = data.get("suggest_claude_cli")
    suggest_claude_cli = Path(raw_suggest).expanduser() if raw_suggest else None
    raw_benchmarks = data.get("benchmarks", {})
    if not isinstance(raw_benchmarks, dict):
        # A present-but-malformed section must error, not silently disable
        # the feature (spec §3: invalid value = load-time error).
        raise ValueError(f"[benchmarks] must be a TOML table, got: {raw_benchmarks!r}")
    raw_url = raw_benchmarks.get("url")
    benchmarks_url = _validate_benchmarks_url(raw_url) if raw_url is not None else None
    if "token" in raw_benchmarks:
        # Phase-2 §3: dispatcher.toml must stay secret-free — an inline
        # token must fail loudly, not work quietly.
        raise ValueError(
            "[benchmarks].token is not a config key: dispatcher.toml never "
            "carries the secret itself — put the token in a 0600 file and "
            "point [benchmarks].token_file at it"
        )
    raw_token_file = raw_benchmarks.get("token_file")
    if raw_token_file is not None and not isinstance(raw_token_file, str):
        raise ValueError(
            f"[benchmarks].token_file must be a string path, got: {raw_token_file!r}"
        )
    benchmarks_token_file = (
        Path(raw_token_file).expanduser() if raw_token_file else None
    )
    if benchmarks_token_file is not None and benchmarks_url is None:
        raise ValueError(
            "[benchmarks].token_file without [benchmarks].url is a "
            "misconfiguration: a token with nothing to spend it on"
        )
    return DispatcherConfig(
        roots=roots,
        maestro_db=maestro_db,
        maestro_home=maestro_home,
        port=int(data.get("port", DEFAULT_PORT)),
        roadmap_dirs=roadmap_dirs,
        tracking_file=tracking_file,
        suggest_claude_cli=suggest_claude_cli,
        benchmarks_url=benchmarks_url,
        benchmarks_token_file=benchmarks_token_file,
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
