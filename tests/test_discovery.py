"""Tests for config loading and project discovery."""

from pathlib import Path

from conftest import make_arbiter, make_atp, make_proctor, make_spec_runner

from dispatcher.core.collectors import COLLECTORS
from dispatcher.core.discovery import DispatcherConfig, discover, load_config


def test_collectors_registry() -> None:
    names = {c.name for c in COLLECTORS}
    assert names == {"atp-platform", "Maestro", "arbiter", "spec-runner", "proctor-a"}


def test_load_config_from_file(tmp_path: Path) -> None:
    cfg = tmp_path / "dispatcher.toml"
    cfg.write_text(
        f'roots = ["{tmp_path}"]\nport = 9999\nmaestro_db = "{tmp_path}/m.db"\n'
    )
    conf = load_config(cfg)
    assert conf.roots == (tmp_path,)
    assert conf.port == 9999
    assert conf.maestro_db == tmp_path / "m.db"


def test_load_config_defaults(tmp_path: Path) -> None:
    conf = load_config(tmp_path / "absent.toml")
    assert len(conf.roots) == 1  # monorepo fallback
    assert conf.port == 8787
    assert conf.maestro_db.name == "maestro.db"


def test_discover_finds_projects(tmp_path: Path) -> None:
    make_arbiter(tmp_path)
    make_spec_runner(tmp_path)
    make_atp(tmp_path)
    found, warnings = discover((tmp_path,), COLLECTORS)
    assert {d.name for d in found} == {"arbiter", "spec-runner", "atp-platform"}
    assert warnings == []


def test_discover_missing_root(tmp_path: Path) -> None:
    found, warnings = discover((tmp_path / "nope",), COLLECTORS)
    assert found == []
    assert len(warnings) == 1


def test_discover_dedupes_by_name(tmp_path: Path) -> None:
    make_proctor(tmp_path)
    root2 = tmp_path / "second"
    root2.mkdir()
    make_proctor(root2)
    found, _ = discover((tmp_path, root2), COLLECTORS)
    assert [d.name for d in found] == ["proctor-a"]


def test_discover_skips_cowork_output(tmp_path: Path) -> None:
    # _cowork_output is dev-only per monorepo rules; even a fully-formed
    # project living under it must never be detected.
    make_proctor(tmp_path / "_cowork_output")
    make_arbiter(tmp_path)
    found, _ = discover((tmp_path,), COLLECTORS)
    assert {d.name for d in found} == {"arbiter"}


def test_config_is_frozen(tmp_path: Path) -> None:
    conf = DispatcherConfig(roots=(tmp_path,), maestro_db=tmp_path / "m.db")
    try:
        conf.port = 1  # type: ignore[misc]
        raise AssertionError("should be frozen")
    except AttributeError:
        pass
