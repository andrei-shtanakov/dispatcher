"""PF-6: two separate guarantees, never folded into one verdict.

`vendored_integrity` — consumer-owned, offline, always available: the vendored
surface under `packages/plan-fields/.../contract/` matches the manifest that
travels with it, and its `PINNED.txt` states a reviewable provenance. It reads
nothing outside dispatcher and never skips.

`upstream_drift` — a producer *observation*: canon in prograph-vault differs,
or does not, from the vendored copy. Available only when a canon checkout is
explicitly provided; otherwise `None`, which means unknown, not in sync.

The two answer different questions — "the pin is intact" and "upstream has not
moved" — and a single `in_sync` cannot carry both. What made this concrete:
the check used to compare against whatever `../prograph-vault` happened to be
checked out to, went green while that checkout sat on a commit other than the
one `PINNED.txt` names, and skipped entirely on CI where the sibling is absent.

Note on the strength of the claim: without a signature or a producer-commit
checkout, integrity proves the vendored surface is internally consistent with
its manifest. The link from that manifest to the vault commit in `PINNED.txt`
is reviewable provenance, not a cryptographic attestation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from dispatcher.core.contracts import check_contracts
from dispatcher.core.models import ContractStatus
from dispatcher.core.roadmap import contract_sync_by_name

_CANON_REL = "authored/contracts/plan-fields/v1"
_VENDORED_REL = "packages/plan-fields/src/plan_fields/contract"
# A synthetic pin for the fabricated copies below — deliberately NOT the
# commit the real copy is vendored from (see PINNED.txt). These tests build
# their own contract directories; using the live pin here would read as a
# second copy of it and get "updated" on the next re-vendor, turning fixtures
# into a duplicate of the thing they are supposed to be independent of.
_PIN = "db6c7a6fd646cfa48d1453b7cfe7f05c3df0ea00"
_SURFACE = {"schema.json": '{"x":1}', "fixtures/valid/basic.md": "- [x] a\n"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _manifest_for(files: dict[str, str]) -> dict:
    surface = [{"path": r, "sha256": _sha(files[r])} for r in sorted(files)]
    h = hashlib.sha256()
    for e in surface:
        h.update(f"{e['path']}\0{e['sha256']}\n".encode())
    return {
        "contract": "plan-fields",
        "contract_version": 1,
        "tree_sha256": h.hexdigest(),
        "surface": surface,
    }


def _pin_text(commit: str = _PIN) -> str:
    return (
        f"source: prograph-vault {_CANON_REL}\n"
        f"commit: {commit}\n"
        "vendored: 2026-07-29\n"
        "note: pinned copy (repo-boundaries vendoring).\n"
    )


def _write_surface(root: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)


def _write_canon(root: Path, files: dict[str, str]) -> None:
    """A vault-side canon directory + its fingerprint."""
    cdir = root / _CANON_REL
    _write_surface(cdir, files)
    (cdir / "manifest.json").write_text(json.dumps(_manifest_for(files)))


_DERIVE = object()  # "generate it", as distinct from "do not write one"
# The real file, not a stand-in: the recorded hash applies to every copy that
# claims to be ours, so a synthetic copy carrying invented meta content would
# be testing a directory the checker is right to reject.
_REAL_CONTRACT = (
    Path(__file__).parent.parent / "packages/plan-fields/src/plan_fields/contract"
)
_DEFAULT_META = {"drift-control.md": (_REAL_CONTRACT / "drift-control.md").read_text()}


def _write_vendored(
    root: Path,
    files: dict[str, str],
    *,
    manifest: object = _DERIVE,
    pin: str | None = None,
    meta: dict[str, str] | None = None,
) -> Path:
    """A consumer-side vendored copy carrying its OWN manifest and pin.

    `meta` writes the files excluded from the fingerprinted surface. Default
    is a realistic copy: a real vendored directory has a drift-control.md, and
    a helper that omitted it would make every test agree that the absence is
    fine.
    """
    vdir = root / _VENDORED_REL
    _write_surface(vdir, files)
    body = _manifest_for(files) if manifest is _DERIVE else manifest
    if body is not None:
        (vdir / "manifest.json").write_text(json.dumps(body))
    if pin is not None:
        (vdir / "PINNED.txt").write_text(pin)
    for name, text in (_DEFAULT_META if meta is None else meta).items():
        (vdir / name).write_text(text)
    return vdir


def _projects(tmp_path: Path, *, with_vault: bool = True) -> dict[str, Path]:
    projects = {"dispatcher": tmp_path / "self"}
    if with_vault:
        projects["prograph-vault"] = tmp_path / "vault"
    return projects


def _row(results: list[ContractStatus], kind: str) -> ContractStatus:
    return next(r for r in results if r.name == "plan-fields-v2" and r.kind == kind)


def _integrity(results: list[ContractStatus]) -> ContractStatus:
    return _row(results, "vendored_integrity")


def _drift(results: list[ContractStatus]) -> ContractStatus:
    return _row(results, "upstream_drift")


def _good_copy(tmp_path: Path) -> Path:
    return _write_vendored(tmp_path / "self", _SURFACE, pin=_pin_text())


# --------------------------------------------------------------------------
# A — vendored integrity: offline, consumer-owned, never skipped
# --------------------------------------------------------------------------


class TestVendoredIntegrity:
    def test_a_copy_matching_its_own_manifest_and_pin_is_intact(
        self, tmp_path: Path
    ) -> None:
        _good_copy(tmp_path)
        row = _integrity(check_contracts(_projects(tmp_path, with_vault=False)))
        assert row.in_sync is True, row.detail
        assert row.detail is None

    def test_it_does_not_read_the_vault_at_all(self, tmp_path: Path) -> None:
        """A wrecked canon must not move a consumer-owned verdict.

        This is the property the whole split exists for: integrity is about
        our copy, and nothing a neighbour does can change it.
        """
        _good_copy(tmp_path)
        _write_canon(tmp_path / "vault", {"schema.json": '{"totally":"different"}'})
        row = _integrity(check_contracts(_projects(tmp_path)))
        assert row.in_sync is True, row.detail

    def test_an_edited_vendored_file_breaks_integrity(self, tmp_path: Path) -> None:
        vdir = _good_copy(tmp_path)
        (vdir / "schema.json").write_text('{"x":2}')
        row = _integrity(check_contracts(_projects(tmp_path, with_vault=False)))
        assert row.in_sync is False
        assert "schema.json" in (row.detail or "")

    def test_a_deleted_vendored_file_breaks_integrity(self, tmp_path: Path) -> None:
        vdir = _good_copy(tmp_path)
        (vdir / "schema.json").unlink()
        row = _integrity(check_contracts(_projects(tmp_path, with_vault=False)))
        assert row.in_sync is False
        assert "schema.json" in (row.detail or "")

    def test_an_extra_vendored_file_breaks_integrity(self, tmp_path: Path) -> None:
        """Coverage runs both ways: per-file hashes alone cannot see an addition.

        A file nobody fingerprinted is a file nobody reviewed, and it ships
        inside the package like the rest of the surface.
        """
        vdir = _good_copy(tmp_path)
        (vdir / "sneaked.json").write_text('{"unreviewed":true}')
        row = _integrity(check_contracts(_projects(tmp_path, with_vault=False)))
        assert row.in_sync is False
        assert "sneaked.json" in (row.detail or "")

    def test_a_manifest_whose_tree_hash_disagrees_with_its_surface_is_refused(
        self, tmp_path: Path
    ) -> None:
        """The fingerprint must certify the list it ships with.

        Editing a surface entry and its file together would otherwise pass
        every per-file check while the tree hash — the thing quoted in review
        and in the vault — silently no longer describes the copy.
        """
        manifest = _manifest_for(_SURFACE)
        manifest["tree_sha256"] = "0" * 64
        _write_vendored(tmp_path / "self", _SURFACE, manifest=manifest, pin=_pin_text())
        row = _integrity(check_contracts(_projects(tmp_path, with_vault=False)))
        assert row.in_sync is False
        assert "tree_sha256" in (row.detail or "")

    def test_an_absent_manifest_is_a_failure_not_an_unknown(
        self, tmp_path: Path
    ) -> None:
        """Our own copy. If its manifest is gone, integrity is broken —
        `None` here would be the skip this slice removes, wearing a new hat."""
        _write_vendored(tmp_path / "self", _SURFACE, manifest=None, pin=_pin_text())
        row = _integrity(check_contracts(_projects(tmp_path, with_vault=False)))
        assert row.in_sync is False
        assert "manifest" in (row.detail or "")

    def test_a_malformed_manifest_is_a_failure(self, tmp_path: Path) -> None:
        vdir = _write_vendored(tmp_path / "self", _SURFACE, pin=_pin_text())
        (vdir / "manifest.json").write_text("{not json")
        row = _integrity(check_contracts(_projects(tmp_path, with_vault=False)))
        assert row.in_sync is False

    def test_a_missing_pin_is_a_failure(self, tmp_path: Path) -> None:
        _write_vendored(tmp_path / "self", _SURFACE, pin=None)
        row = _integrity(check_contracts(_projects(tmp_path, with_vault=False)))
        assert row.in_sync is False
        assert "PINNED.txt" in (row.detail or "")

    def test_a_pin_without_a_commit_is_a_failure(self, tmp_path: Path) -> None:
        """Provenance that names no revision is prose, not provenance."""
        _write_vendored(
            tmp_path / "self",
            _SURFACE,
            pin=f"source: prograph-vault {_CANON_REL}\nvendored: 2026-07-29\n",
        )
        row = _integrity(check_contracts(_projects(tmp_path, with_vault=False)))
        assert row.in_sync is False
        assert "commit" in (row.detail or "")

    def test_a_pin_whose_commit_is_not_a_sha_is_a_failure(self, tmp_path: Path) -> None:
        _write_vendored(tmp_path / "self", _SURFACE, pin=_pin_text("HEAD"))
        row = _integrity(check_contracts(_projects(tmp_path, with_vault=False)))
        assert row.in_sync is False

    def test_a_pin_without_a_source_is_a_failure(self, tmp_path: Path) -> None:
        _write_vendored(tmp_path / "self", _SURFACE, pin=f"commit: {_PIN}\n")
        row = _integrity(check_contracts(_projects(tmp_path, with_vault=False)))
        assert row.in_sync is False
        assert "source" in (row.detail or "")


# --------------------------------------------------------------------------
# B — upstream drift: an observation, unknown unless canon is handed over
# --------------------------------------------------------------------------


class TestUpstreamDrift:
    def test_without_a_canon_checkout_it_is_unknown_not_in_sync(
        self, tmp_path: Path
    ) -> None:
        _good_copy(tmp_path)
        row = _drift(check_contracts(_projects(tmp_path, with_vault=False)))
        assert row.in_sync is None
        assert "no canon" in (row.detail or "")

    def test_it_never_falls_back_to_a_sibling_on_disk(self) -> None:
        """The defect that started this: the old check silently compared
        against whatever `../prograph-vault` happened to be checked out to —
        so the same commit of dispatcher answered differently on two machines,
        and on CI, where no sibling exists, the check vanished into a skip.

        Run against the real repository with nothing handed over. On a machine
        where the vault IS cloned next door, a fallback would answer True here.
        """
        assert _drift(check_contracts({})).in_sync is None

    def test_matching_canon_is_observed_as_no_drift(self, tmp_path: Path) -> None:
        _good_copy(tmp_path)
        _write_canon(tmp_path / "vault", _SURFACE)
        row = _drift(check_contracts(_projects(tmp_path)))
        assert row.in_sync is True, row.detail

    def test_a_changed_canon_file_is_observed_as_drift(self, tmp_path: Path) -> None:
        _good_copy(tmp_path)
        _write_canon(tmp_path / "vault", {**_SURFACE, "schema.json": '{"x":2}'})
        row = _drift(check_contracts(_projects(tmp_path)))
        assert row.in_sync is False
        assert "schema.json" in (row.detail or "")

    def test_a_stale_canon_manifest_never_certifies(self, tmp_path: Path) -> None:
        _good_copy(tmp_path)
        _write_canon(tmp_path / "vault", _SURFACE)
        (tmp_path / "vault" / _CANON_REL / "schema.json").write_text('{"x":999}')
        row = _drift(check_contracts(_projects(tmp_path)))
        assert row.in_sync is False
        assert "stale" in (row.detail or "")

    def test_an_added_canon_file_makes_the_manifest_stale(self, tmp_path: Path) -> None:
        _good_copy(tmp_path)
        _write_canon(tmp_path / "vault", _SURFACE)
        (tmp_path / "vault" / _CANON_REL / "extra.md").write_text("new\n")
        row = _drift(check_contracts(_projects(tmp_path)))
        assert row.in_sync is False
        assert "extra.md" in (row.detail or "")

    def test_a_malformed_canon_manifest_is_unknown_not_drift(
        self, tmp_path: Path
    ) -> None:
        """Unreadable upstream is not evidence that upstream changed."""
        _good_copy(tmp_path)
        _write_canon(tmp_path / "vault", _SURFACE)
        (tmp_path / "vault" / _CANON_REL / "manifest.json").write_text(
            json.dumps({"surface": ["not-a-dict"]})
        )
        row = _drift(check_contracts(_projects(tmp_path)))
        assert row.in_sync is None
        assert "malformed" in (row.detail or "")


# --------------------------------------------------------------------------
# Governance must read the deterministic half, never the observation
# --------------------------------------------------------------------------


def test_governance_folds_integrity_only_not_the_observation(
    tmp_path: Path,
) -> None:
    """Roadmap evidence for plan-fields-v2 must not go unknown just because
    no canon checkout was handed over — that would make every ordinary run
    report less than it knows."""
    _good_copy(tmp_path)
    results = check_contracts(_projects(tmp_path, with_vault=False))
    assert _drift(results).in_sync is None  # the observation is unknown
    assert (
        contract_sync_by_name(results, kind="vendored_integrity")["plan-fields-v2"]
        is True
    )


def test_governance_reports_drift_when_integrity_is_broken(tmp_path: Path) -> None:
    """Non-vacuity: the fold is not hardcoded to True."""
    vdir = _good_copy(tmp_path)
    (vdir / "schema.json").write_text('{"x":2}')
    results = check_contracts(_projects(tmp_path, with_vault=False))
    assert (
        contract_sync_by_name(results, kind="vendored_integrity")["plan-fields-v2"]
        is False
    )


# --------------------------------------------------------------------------
# The real copy in this repository
# --------------------------------------------------------------------------


def test_the_real_vendored_copy_matches_its_own_manifest_and_pin() -> None:
    """Dogfood, offline, no skip. Replaces the old canon-comparison test.

    The claim is exactly what it says: dispatcher's vendored plan-fields
    surface is internally consistent with the manifest travelling beside it,
    and that manifest records a reviewable provenance. Whether the vault has
    moved since is a different question, answered by the advisory workflow.
    """
    results = check_contracts({})  # no canon handed over — and none needed
    row = _integrity(results)
    assert row.in_sync is True, row.detail
    assert _drift(results).in_sync is None


def test_an_unreadable_vendored_file_says_so_not_hash_mismatch(
    tmp_path: Path, monkeypatch
) -> None:
    """ "Differs from its fingerprint" would send a reviewer hunting a phantom.

    A file that cannot be read has no hash to disagree with; the two failures
    need different sentences because they need different fixes.
    """
    from dispatcher.core import contracts as mod

    vdir = _write_vendored(tmp_path / "self", _SURFACE, pin=_pin_text())
    real = mod._sha256
    monkeypatch.setattr(
        mod, "_sha256", lambda p: None if p.name == "schema.json" else real(p)
    )
    row = _integrity(check_contracts(_projects(tmp_path, with_vault=False)))
    assert row.in_sync is False
    assert "unreadable" in (row.detail or "")
    assert "schema.json" in (row.detail or "")
    assert vdir.exists()


# --------------------------------------------------------------------------
# The files EXCLUDED from the fingerprinted surface still need a check
# --------------------------------------------------------------------------
#
# The exclusion is legitimate — `manifest.json` cannot hash itself, and
# `drift-control.md`/`PINNED.txt` are policy and provenance rather than
# contract — but "excluded from the fingerprint" quietly became "checked by
# nothing". It has now cost twice: `PINNED.txt` could name a commit no one
# verified (closed in #99), and a `drift-control.md` describing the very
# folded check #99 removed shipped inside the package for a day while both
# guarantees stayed green, because neither can see it.


def test_the_exclusion_set_cannot_grow_without_a_declared_check() -> None:
    """Adding a fourth excluded file must be impossible to do silently.

    `_PF_META` is derived from the policy tables rather than written out, so a
    new exclusion has to be declared as either hash-checked or
    structurally-checked before it can exist. This test pins the current
    membership on top, so growing it is a deliberate edit here too.
    """
    from dispatcher.core import contracts as mod

    assert mod._PF_META == {"manifest.json", "drift-control.md", "PINNED.txt"}
    # every excluded name is covered by exactly one policy, none left over
    assert mod._PF_META == set(mod._PF_META_HASHES) | mod._PF_META_STRUCTURAL
    assert not set(mod._PF_META_HASHES) & mod._PF_META_STRUCTURAL


def test_the_recorded_hash_matches_the_real_vendored_drift_control() -> None:
    """A re-vendor that updates the file and forgets the pin fails here.

    Without this the recorded hash drifts from the copy it describes and the
    check certifies a file nobody compared — the same shape as a stale
    manifest certifying a surface it no longer matches.
    """
    from dispatcher.core import contracts as mod

    actual = hashlib.sha256(
        (
            Path(__file__).parent.parent
            / "packages/plan-fields/src/plan_fields/contract/drift-control.md"
        ).read_bytes()
    ).hexdigest()
    assert mod._PF_META_HASHES["drift-control.md"] == actual


def test_a_tampered_excluded_file_breaks_integrity(tmp_path: Path) -> None:
    """The fingerprint cannot see it, so the recorded hash has to."""
    vdir = _write_vendored(tmp_path / "self", _SURFACE, pin=_pin_text())
    (vdir / "drift-control.md").write_text("tampered\n")
    row = _integrity(check_contracts(_projects(tmp_path, with_vault=False)))
    assert row.in_sync is False
    assert "drift-control.md" in (row.detail or "")


def test_a_missing_excluded_file_breaks_integrity(tmp_path: Path) -> None:
    _write_vendored(tmp_path / "self", _SURFACE, pin=_pin_text(), meta={})
    row = _integrity(check_contracts(_projects(tmp_path, with_vault=False)))
    assert row.in_sync is False
    assert "drift-control.md" in (row.detail or "")


def test_the_pin_must_name_the_contract_the_manifest_declares(
    tmp_path: Path,
) -> None:
    """PINNED.txt is checked against the manifest's own provenance, not just
    for shape: a 40-hex commit under a `source:` naming some other contract is
    well-formed and still wrong."""
    _write_vendored(
        tmp_path / "self",
        _SURFACE,
        pin=(
            "source: prograph-vault authored/contracts/SOMETHING-ELSE/v1\n"
            f"commit: {_PIN}\n"
        ),
    )
    row = _integrity(check_contracts(_projects(tmp_path, with_vault=False)))
    assert row.in_sync is False
    assert "plan-fields" in (row.detail or "")


def test_the_manifest_shape_and_its_exclusion_note_are_pinned() -> None:
    """`manifest.json` is the one file that cannot hash itself.

    So its identity is asserted structurally instead: the contract it names,
    the fingerprint it carries, and — the part that matters here — that its
    own note still describes exactly the three exclusions the checker
    implements. A canon that started excluding a fourth file would otherwise
    pass silently while our policy table said three.
    """
    manifest = json.loads(
        (
            Path(__file__).parent.parent
            / "packages/plan-fields/src/plan_fields/contract/manifest.json"
        ).read_text()
    )
    assert set(manifest) == {
        "contract",
        "contract_version",
        "schema_id",
        "surface_note",
        "tree_sha256",
        "surface",
    }
    assert manifest["contract"] == "plan-fields"
    assert manifest["contract_version"] == 2
    # the normative surface must not have moved: this slice re-vendors meta
    # only, and a changed fingerprint here means the scope slipped
    assert manifest["tree_sha256"] == (
        "e98073e14e1de5000981550013396d5f66208301841eb659b4ebff624a871b6f"
    )
    note = manifest["surface_note"]
    for excluded in ("manifest.json", "drift-control.md", "PINNED.txt"):
        assert excluded in note, note


class TestManifestIdentity:
    """The manifest is the authority the pin cross-check leans on.

    So its own identity has to be checked by the product, not only by a test
    against the real file: a manifest that keeps a self-consistent surface and
    tree hash while declaring a different contract — or none — would pass every
    other check and quietly disarm the cross-reference that depends on it.
    """

    def _with_manifest(self, tmp_path: Path, **changes: object) -> None:
        manifest = {**_manifest_for(_SURFACE), **changes}
        _write_vendored(tmp_path / "self", _SURFACE, manifest=manifest, pin=_pin_text())

    def test_a_manifest_declaring_another_contract_is_refused(
        self, tmp_path: Path
    ) -> None:
        self._with_manifest(tmp_path, contract="something-else")
        row = _integrity(check_contracts(_projects(tmp_path, with_vault=False)))
        assert row.in_sync is False
        assert "plan-fields" in (row.detail or "")

    def test_a_manifest_with_no_contract_name_is_refused(self, tmp_path: Path) -> None:
        """Not merely "the cross-check is skipped": a missing declaration is
        the case that made the cross-check meaningless rather than absent."""
        self._with_manifest(tmp_path, contract=None)
        row = _integrity(check_contracts(_projects(tmp_path, with_vault=False)))
        assert row.in_sync is False

    def test_a_manifest_at_another_contract_version_is_refused(
        self, tmp_path: Path
    ) -> None:
        self._with_manifest(tmp_path, contract_version=2)
        row = _integrity(check_contracts(_projects(tmp_path, with_vault=False)))
        assert row.in_sync is False
        assert "version" in (row.detail or "")
