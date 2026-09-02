"""BEH-14 (WS-dispatcher-229): identical frozen inputs yield identical results.

Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-14

Given unchanged plan inputs, provenance and a frozen workspace manifest, the
fleet analysis must produce the same owner verdict, the same set and count of
diagnostics, the same node URI/provenance and the same reporter-facing
classification across repeated runs — and normalisation must go through the
one shared manifest identity model (``ManifestIndex.resolve_ref``), never a
second, independently re-derived algorithm (NFR-02).
"""

from __future__ import annotations

from plan_fields import (
    ManifestIndex,
    RepoInput,
    canonical_dumps,
    parse_fleet,
    repo_owned_node_ids,
    repo_owner_verdicts,
    validate_document,
)

_TEXT = (
    "- [ ] self canonical @id:a @owner:repo:dispatcher\n"
    "- [ ] self git_dir @id:b @owner:repo:legacy-checkout-dir\n"
    "- [ ] external @id:c @owner:repo:maestro\n"
    "- [ ] unknown @id:d @owner:repo:ghost\n"
    "- [ ] typed person @id:e @owner:github:dispatcher\n"
)


def _index() -> ManifestIndex:
    # A fresh ManifestIndex built the same way each call — proving the result
    # depends on the frozen manifest CONTENT, not on reusing one Python object.
    return ManifestIndex(
        frozenset({"dispatcher", "maestro"}), {"legacy-checkout-dir": "dispatcher"}
    )


def _inputs() -> list[RepoInput]:
    # Fresh RepoInput instances each call, same repos/text/commit — proving no
    # hidden mutable state is carried between runs via the input objects.
    return [
        RepoInput("dispatcher", _TEXT, commit="deadbeef"),
        RepoInput("maestro", "- [ ] noop @id:z @owner:TBD\n", commit="cafef00d"),
    ]


def test_repeated_parse_fleet_calls_produce_the_identical_document() -> None:
    doc1 = parse_fleet(_inputs(), _index())
    doc2 = parse_fleet(_inputs(), _index())
    validate_document(doc1)
    validate_document(doc2)
    assert doc1 == doc2


def test_repeated_calls_produce_byte_identical_canonical_json() -> None:
    doc1 = parse_fleet(_inputs(), _index())
    doc2 = parse_fleet(_inputs(), _index())
    assert canonical_dumps(doc1) == canonical_dumps(doc2)


def test_owner_verdict_set_and_count_are_stable_across_runs() -> None:
    doc1 = parse_fleet(_inputs(), _index())
    doc2 = parse_fleet(_inputs(), _index())

    def owner_diags(doc: dict) -> list[tuple[str, str]]:
        return sorted(
            (d["code"], d["subject_uri"])
            for d in doc["diagnostics"]
            if d["code"].startswith("PF-OWNER")
        )

    diags1 = owner_diags(doc1)
    diags2 = owner_diags(doc2)
    assert diags1 == diags2
    assert diags1 == [
        ("PF-OWNER-REPO-SELF", "todo://dispatcher/a"),
        ("PF-OWNER-REPO-SELF", "todo://dispatcher/b"),
        ("PF-OWNER-REPO-UNKNOWN", "todo://dispatcher/d"),
    ]


def test_node_uri_and_provenance_are_stable_across_runs() -> None:
    doc1 = parse_fleet(_inputs(), _index())
    doc2 = parse_fleet(_inputs(), _index())
    by_uri1 = {n["node_id"]: n["provenance"] for n in doc1["nodes"]}
    by_uri2 = {n["node_id"]: n["provenance"] for n in doc2["nodes"]}
    assert by_uri1 == by_uri2
    assert set(by_uri1) == {
        "todo://dispatcher/a",
        "todo://dispatcher/b",
        "todo://dispatcher/c",
        "todo://dispatcher/d",
        "todo://dispatcher/e",
        "todo://maestro/z",
    }


def test_reporter_facing_classification_is_stable_across_runs() -> None:
    doc1 = parse_fleet(_inputs(), _index())
    doc2 = parse_fleet(_inputs(), _index())
    assert repo_owner_verdicts(doc1) == repo_owner_verdicts(doc2)
    assert repo_owner_verdicts(doc1) == {
        "todo://dispatcher/a": "self",
        "todo://dispatcher/b": "self",
        "todo://dispatcher/c": "external",
        "todo://dispatcher/d": "unknown",
    }
    assert repo_owned_node_ids(doc1) == repo_owned_node_ids(doc2)
    assert repo_owned_node_ids(doc1) == ["todo://dispatcher/c"]


def test_determinism_holds_across_differently_ordered_repo_inputs() -> None:
    # parse_fleet takes a Sequence[RepoInput]; a caller that discovers repos
    # via filesystem/manifest iteration has no control over that order. The
    # canonical result must not depend on it — only on frozen input content.
    doc_forward = parse_fleet(_inputs(), _index())
    doc_reversed = parse_fleet(list(reversed(_inputs())), _index())
    assert doc_forward == doc_reversed
