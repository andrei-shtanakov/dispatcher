from pathlib import Path

import pytest

from dispatcher.core.dag_subset import Accepted, Rejected, classify_dag_text

FIXTURES = Path(__file__).parent / "fixtures" / "dag_subset"


def load(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_minimal_mode1_is_accepted():
    verdict = classify_dag_text(load("mode1-minimal.yaml"))
    assert isinstance(verdict, Accepted)
    assert verdict.repo_path == "/home/user/labs/demo"
    assert verdict.repo_url is None


def test_repo_url_names_a_mode1_repo_too():
    # submit's shipped semantics (test_run_request.py pins the same)
    verdict = classify_dag_text(load("mode1-repo-url.yaml"))
    assert isinstance(verdict, Accepted)
    assert verdict.repo_url == "git@github.com:andrei-shtanakov/demo.git"


def test_repo_url_wins_when_both_are_present():
    # _reconcile_repo precedence, mirrored
    verdict = classify_dag_text(
        "repo: /home/user/labs/demo\n"
        "repo_url: git@github.com:andrei-shtanakov/demo.git\n"
        "tasks: []\n"
    )
    assert isinstance(verdict, Accepted)
    assert verdict.repo_url == "git@github.com:andrei-shtanakov/demo.git"


@pytest.mark.parametrize(
    "name",
    [
        "mode2-orchestrator.yaml",
        "only-workstreams.yaml",
        "not-yaml.yaml",
        "no-tasks.yaml",
        "tasks-not-list.yaml",
        "no-repo-naming.yaml",
        "repo-not-string.yaml",
        "top-level-not-mapping.yaml",
    ],
)
def test_rejections_are_named(name: str):
    verdict = classify_dag_text(load(name))
    assert isinstance(verdict, Rejected)
    assert verdict.reason  # non-empty, human-readable


def test_empty_repo_url_falls_back_to_repo():
    # _reconcile_repo: empty string repo_url is treated as absent
    verdict = classify_dag_text('repo: /home/user/labs/demo\nrepo_url: ""\ntasks: []\n')
    assert isinstance(verdict, Accepted)
    assert verdict.repo_path == "/home/user/labs/demo"
    assert verdict.repo_url is None


def test_whitespace_only_repo_url_falls_back_to_repo():
    # _reconcile_repo: whitespace-only repo_url is treated as absent
    verdict = classify_dag_text(
        'repo: /home/user/labs/demo\nrepo_url: "  "\ntasks: []\n'
    )
    assert isinstance(verdict, Accepted)
    assert verdict.repo_path == "/home/user/labs/demo"
    assert verdict.repo_url is None


def test_non_string_repo_url_falls_back_to_repo():
    # _reconcile_repo: non-string repo_url is treated as absent
    verdict = classify_dag_text("repo: /home/user/labs/demo\nrepo_url: 3\ntasks: []\n")
    assert isinstance(verdict, Accepted)
    assert verdict.repo_path == "/home/user/labs/demo"
    assert verdict.repo_url is None


def test_oversized_source_is_rejected_before_parsing():
    verdict = classify_dag_text("a: " + "b" * (2 * 1024 * 1024))
    assert isinstance(verdict, Rejected)


def test_alias_bomb_is_rejected_at_the_event_stream():
    # billion-laughs expands INSIDE the 1MiB source cap; safe_load in PyYAML
    # does expand aliases, so the event-scan phase must refuse them before
    # any construction happens
    bomb = "a: &a [x,x,x,x,x,x,x,x]\n" + "\n".join(
        f"{chr(98 + i)}: &{chr(98 + i)} [{','.join(['*' + chr(97 + i)] * 8)}]"
        for i in range(6)
    )
    verdict = classify_dag_text(bomb)
    assert isinstance(verdict, Rejected)
    assert "alias" in verdict.reason


def test_deeply_nested_yaml_is_rejected_not_crashed():
    # RecursionError is not a yaml.YAMLError: without an explicit catch a
    # <1MiB doc of thousands of nested lists crashes the caller instead of
    # classifying as Rejected (fail-closed).
    bomb = "[" * 5000 + "]" * 5000
    verdict = classify_dag_text(bomb)
    assert isinstance(verdict, Rejected)
    assert verdict.reason
