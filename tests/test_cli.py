"""Tests for the CLI argument parsing."""

from pathlib import Path

from dispatcher.cli import build_parser


def test_serve_defaults() -> None:
    args = build_parser().parse_args(["serve"])
    assert args.command == "serve"
    assert args.port is None
    assert args.config is None


def test_serve_overrides(tmp_path: Path) -> None:
    cfg = tmp_path / "d.toml"
    args = build_parser().parse_args(["serve", "--port", "9000", "--config", str(cfg)])
    assert args.port == 9000
    assert args.config == cfg


def test_tui_subcommand_parses() -> None:
    args = build_parser().parse_args(["tui", "--config", "x.toml"])
    assert args.command == "tui"
    assert args.config == Path("x.toml")
