"""Tests for the contract drift checker."""

from pathlib import Path

from conftest import make_arbiter, make_atp, make_spec_runner

from dispatcher.core.contracts import check_contracts


def test_drift_detected(tmp_path: Path) -> None:
    atp = make_atp(tmp_path)
    arb = make_arbiter(tmp_path)  # vendored copy differs from canon
    results = check_contracts({"atp-platform": atp, "arbiter": arb})
    catalog = next(r for r in results if r.name == "agents-catalog")
    assert catalog.in_sync is False


def test_in_sync(tmp_path: Path) -> None:
    atp = make_atp(tmp_path)
    arb = make_arbiter(tmp_path)
    canon = (atp / "method" / "agents-catalog.toml").read_text()
    (arb / "config" / "agents-catalog.toml").write_text(canon)
    results = check_contracts({"atp-platform": atp, "arbiter": arb})
    catalog = next(r for r in results if r.name == "agents-catalog")
    assert catalog.in_sync is True


def test_canon_missing(tmp_path: Path) -> None:
    arb = make_arbiter(tmp_path)
    results = check_contracts({"arbiter": arb})
    catalog = next(r for r in results if r.name == "agents-catalog")
    assert catalog.in_sync is None


def test_schema_listing(tmp_path: Path) -> None:
    sr = make_spec_runner(tmp_path)
    results = check_contracts({"spec-runner": sr})
    schemas = [r for r in results if r.detail == "published schema"]
    assert [s.name for s in schemas] == ["status.schema.json"]
