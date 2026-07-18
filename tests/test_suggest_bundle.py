"""DESIGN-901: deterministic, redacted suggestion bundle."""

from typing import Any

from dispatcher.core.models import ProjectSnapshot
from dispatcher.core.spec_runner_config import (
    TYPED_DEFAULTS,
    ProjectSpecRunnerConfig,
    TypedField,
)
from dispatcher.core.suggest_bundle import (
    INSTRUCTION,
    PROMPT_VERSION,
    build_suggest_bundle,
)


def _cfg(
    project: str, explicit: dict[str, Any] | None = None, mtime: float = 1000.0
) -> ProjectSpecRunnerConfig:
    explicit = explicit or {}
    typed = {
        name: TypedField(value=explicit.get(name, default), explicit=name in explicit)
        for name, default in TYPED_DEFAULTS.items()
    }
    return ProjectSpecRunnerConfig(
        project=project,
        project_yaml_path=f"/w/{project}/project.yaml",
        base_mtime=mtime,
        typed=typed,
        extra_executor_config={},
        extra_explicit=False,
    )


def test_bundle_shape_and_requested_fields() -> None:
    cfg = _cfg("steward", {"max_retries": 5})
    bundle = build_suggest_bundle(cfg, peers=[], snapshot=None)
    assert bundle["instruction"] == INSTRUCTION
    assert bundle["prompt_version"] == PROMPT_VERSION
    # explicit field is NOT requested; every default-provenance field is
    assert "max_retries" not in bundle["requested_fields"]
    assert "claude_model" in bundle["requested_fields"]
    assert bundle["field_schema"]["max_retries"] == {"type": "int", "default": 3}
    assert bundle["field_schema"]["auto_commit"] == {"type": "bool", "default": True}
    assert bundle["current_config"]["max_retries"] == {"value": 5, "explicit": True}
    assert bundle["project"] == {"name": "steward"}


def test_bundle_has_no_roadmap_or_extra() -> None:
    bundle = build_suggest_bundle(_cfg("steward"), peers=[], snapshot=None)
    assert "roadmap" not in bundle  # negative pin — cut by design (§3)
    assert "extra_executor_config" not in bundle
    for key in bundle:
        assert key in {
            "instruction",
            "prompt_version",
            "requested_fields",
            "field_schema",
            "current_config",
            "peers",
            "project",
        }


def test_bundle_description_from_snapshot() -> None:
    snap = ProjectSnapshot(
        name="Maestro",
        path="/w/steward",
        description="Steward governs specs.",
        description_source="readme",
    )
    bundle = build_suggest_bundle(_cfg("steward"), peers=[], snapshot=snap)
    assert bundle["project"] == {
        "name": "steward",
        "description": "Steward governs specs.",
        "description_source": "readme",
    }


def test_peers_distribution_list_top5_other_and_explicit_count() -> None:
    peers = (
        [_cfg(f"p{i}", {"max_retries": 5}) for i in range(3)]  # 3x explicit 5
        + [_cfg(f"q{i}") for i in range(2)]  # 2x default 3
        + [
            _cfg("r0", {"max_retries": 7}),
            _cfg("r1", {"max_retries": 8}),
            _cfg("r2", {"max_retries": 9}),
            _cfg("r3", {"max_retries": 10}),
            _cfg("r4", {"max_retries": 11}),
        ]
    )
    bundle = build_suggest_bundle(_cfg("steward"), peers=peers, snapshot=None)
    dist = bundle["peers"]["max_retries"]
    assert dist[0] == {"value": 5, "count": 3, "explicit_count": 3}
    assert dist[1] == {"value": 3, "count": 2, "explicit_count": 0}
    # list encoding (not an object): bool/int values stay typed
    assert isinstance(dist, list) and isinstance(dist[0]["value"], int)
    # top-5 entries + {"other": N} tail for the 5 distinct singletons - 3 kept
    values = [d["value"] for d in dist if "value" in d]
    assert len(values) == 5
    assert dist[-1] == {"other": 2}


def test_peers_cap_top15_by_base_mtime() -> None:
    # 5 oldest carry a marker value: cap must drop exactly them
    old = [_cfg(f"o{i}", {"max_retries": 99}, mtime=float(i)) for i in range(5)]
    fresh = [_cfg(f"f{i}", {"max_retries": 5}, mtime=float(100 + i)) for i in range(15)]
    bundle = build_suggest_bundle(_cfg("steward"), peers=old + fresh, snapshot=None)
    (row,) = bundle["peers"]["max_retries"]
    assert row == {"value": 5, "count": 15, "explicit_count": 15}  # no 99 row


def test_bundle_masks_secrets_in_values_and_description() -> None:
    cfg = _cfg("steward", {"claude_command": "run --key sk-abcdef123456"})
    snap = ProjectSnapshot(
        name="Maestro",
        path="/w/steward",
        description="Uses token ghp_abcdef123456 internally.",
        description_source="readme",
    )
    peers = [_cfg("p0", {"test_command": "curl https://u:pass@host/x"})]
    bundle = build_suggest_bundle(cfg, peers=peers, snapshot=snap)
    assert "sk-abcdef123456" not in str(bundle)
    assert "ghp_abcdef123456" not in str(bundle)
    assert "u:pass@" not in str(bundle)


def test_bundle_deterministic() -> None:
    peers = [_cfg("b"), _cfg("a")]
    one = build_suggest_bundle(_cfg("s"), peers=peers, snapshot=None)
    two = build_suggest_bundle(_cfg("s"), peers=list(reversed(peers)), snapshot=None)
    assert one == two
