"""Tests for config loading and project discovery."""

from pathlib import Path

import pytest
from conftest import make_arbiter, make_atp, make_proctor, make_spec_runner

from dispatcher.core.collectors import COLLECTORS
from dispatcher.core.discovery import DispatcherConfig, discover, load_config


def test_collectors_registry() -> None:
    names = {c.name for c in COLLECTORS}
    assert names == {
        "atp-platform",
        "Maestro",
        "arbiter",
        "spec-runner",
        "proctor",
        "impresario",
    }


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
    assert [d.name for d in found] == ["proctor"]


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


def test_load_config_suggest_claude_cli(tmp_path: Path) -> None:
    cfg_file = tmp_path / "dispatcher.toml"
    cfg_file.write_text('suggest_claude_cli = "/opt/bin/claude"\n')
    cfg = load_config(cfg_file)
    assert cfg.suggest_claude_cli == Path("/opt/bin/claude")
    cfg_file.write_text("")
    assert load_config(cfg_file).suggest_claude_cli is None


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "dispatcher.toml"
    p.write_text(body)
    return p


def test_absent_benchmarks_section_yields_none(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, "roots = []\n"))
    assert config.benchmarks_url is None


def test_valid_url_is_kept_with_trailing_slash_stripped(tmp_path: Path) -> None:
    config = load_config(
        _write(tmp_path, '[benchmarks]\nurl = "http://127.0.0.1:8000/"\n')
    )
    assert config.benchmarks_url == "http://127.0.0.1:8000"


def test_base_path_is_allowed(tmp_path: Path) -> None:
    config = load_config(
        _write(tmp_path, '[benchmarks]\nurl = "https://host.example/atp/"\n')
    )
    assert config.benchmarks_url == "https://host.example/atp"


@pytest.mark.parametrize(
    "bad",
    [
        "ftp://host",  # wrong scheme
        "http://",  # no host
        "/api",  # not absolute
        "http://host/x?query=1",  # query forbidden
        "http://host/x#frag",  # fragment forbidden
        "http://user:pw@host/",  # userinfo forbidden
    ],
)
def test_invalid_url_is_a_load_time_error(tmp_path: Path, bad: str) -> None:
    path = _write(tmp_path, f'[benchmarks]\nurl = "{bad}"\n')
    with pytest.raises(ValueError):
        load_config(path)
