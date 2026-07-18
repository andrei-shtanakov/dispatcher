"""DESIGN-801: README-first description extraction with metadata fallback."""

from pathlib import Path

from dispatcher.core.descriptions import extract_project_description


def test_readme_first_meaningful_paragraph(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "# Title\n\n"
        "[![build](https://x/badge.svg)](https://x)\n\n"
        '<p align="center"><img src="logo.png"></p>\n\n'
        "Dispatcher is an ecosystem dashboard.\n"
        "It watches every project.\n\n"
        "Second paragraph must not leak.\n"
    )
    text, source = extract_project_description(tmp_path)
    assert text == "Dispatcher is an ecosystem dashboard. It watches every project."
    assert source == "readme"


def test_readme_all_noise_falls_back_to_pyproject(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Only a title\n\n![badge](x.svg)\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndescription = "Terse authoritative line."\n'
    )
    text, source = extract_project_description(tmp_path)
    assert text == "Terse authoritative line."
    assert source == "pyproject"


def test_package_json_is_last_fallback(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"description": "From npm metadata."}')
    text, source = extract_project_description(tmp_path)
    assert text == "From npm metadata."
    assert source == "package.json"


def test_no_sources_returns_none(tmp_path: Path) -> None:
    assert extract_project_description(tmp_path) == (None, None)


def test_rst_readme_skips_title_underline(tmp_path: Path) -> None:
    (tmp_path / "README.rst").write_text(
        "My Project\n==========\n\nAn rst-described project.\n"
    )
    text, source = extract_project_description(tmp_path)
    assert text == "An rst-described project."
    assert source == "readme"


def test_extensionless_readme_case_insensitive(tmp_path: Path) -> None:
    (tmp_path / "readme").write_text("Plain readme text.\n")
    assert extract_project_description(tmp_path) == ("Plain readme text.", "readme")


def test_non_utf8_readme_degrades_to_next_source(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_bytes(b"\xff\xfe broken")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndescription = "Fallback wins."\n'
    )
    assert extract_project_description(tmp_path) == ("Fallback wins.", "pyproject")


def test_oversized_readme_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("x" * (256 * 1024 + 1))
    assert extract_project_description(tmp_path) == (None, None)


def test_trim_is_360_word_boundary_with_ellipsis(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(("word " * 100).strip() + "\n")
    text, _ = extract_project_description(tmp_path)
    assert text is not None
    assert len(text) <= 361  # 360 + "…"
    assert text.endswith("…")
    assert not text[:-1].endswith(" ")  # cut on a word boundary, then rstrip
