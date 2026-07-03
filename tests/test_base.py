"""Tests for shared collector helpers."""

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from dispatcher.core.collectors.base import (
    SourceReadError,
    coerce_str,
    mask_secrets,
    newest_mtime,
    read_otel_errors,
    read_rows,
    shallow_summary,
    table_names,
    version_check,
)


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT);"
        "INSERT INTO t (name) VALUES ('a'), ('b');"
    )
    conn.commit()
    conn.close()


def test_read_rows_returns_dicts(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    _make_db(db)
    rows = read_rows(db, "SELECT id, name FROM t ORDER BY id")
    assert rows == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]


def test_read_rows_is_readonly(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    _make_db(db)
    with pytest.raises(SourceReadError):
        read_rows(db, "INSERT INTO t (name) VALUES ('c')")


def test_read_rows_missing_db(tmp_path: Path) -> None:
    with pytest.raises(SourceReadError):
        read_rows(tmp_path / "nope.db", "SELECT 1")


def test_read_rows_retries_through_lock(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    _make_db(db)
    locker = sqlite3.connect(db, check_same_thread=False)
    locker.execute("BEGIN EXCLUSIVE")

    def release() -> None:
        time.sleep(0.3)
        locker.rollback()
        locker.close()

    t = threading.Thread(target=release)
    t.start()
    rows = read_rows(db, "SELECT count(*) AS n FROM t")
    t.join()
    assert rows[0]["n"] == 2


def test_table_names(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    _make_db(db)
    assert "t" in table_names(db)


def test_version_check() -> None:
    assert version_check("d", "1", "1").ok is True
    assert version_check("d", "2", "1").ok is False
    assert version_check("d", None, "1").ok is None


def test_mask_secrets_by_key_and_value() -> None:
    data = {
        "api_key": "abc123",
        "nested": {"bot_token": "xyz", "host": "ok"},
        "url": "nats://user:pass@host:4222",
        "auth": "Bearer sk-live-verysecret",
        "plain": "hello",
    }
    masked = mask_secrets(data)
    assert masked["api_key"] == "***"
    assert masked["nested"]["bot_token"] == "***"
    assert masked["nested"]["host"] == "ok"
    assert "user:pass" not in masked["url"]
    assert "verysecret" not in masked["auth"]
    assert masked["plain"] == "hello"
    assert mask_secrets({"credentials": {"user": "u"}})["credentials"] == "***"


def test_shallow_summary_collapses_containers() -> None:
    out = shallow_summary({"a": 1, "b": [1, 2, 3], "c": {"x": 1}})
    assert out["a"] == 1
    assert out["b"] == "<3 items>"
    assert out["c"] == "<1 items>"


def test_read_otel_errors(tmp_path: Path) -> None:
    run = tmp_path / "logs" / "01AAAAAAAAAAAAAAAAAAAAAAAA"
    run.mkdir(parents=True)
    lines = [
        {"SeverityNumber": 9, "Body": "info", "Resource": {}},
        {
            "SeverityNumber": 17,
            "SeverityText": "ERROR",
            "Body": "boom",
            "Timestamp": "1719999999000000000",
            "Attributes": {"pipeline_id": "01AAA"},
            "Resource": {"service.name": "maestro"},
        },
        "not json at all",
    ]
    (run / "maestro-1.jsonl").write_text(
        "\n".join(item if isinstance(item, str) else json.dumps(item) for item in lines)
    )
    events = read_otel_errors(tmp_path / "logs")
    assert len(events) == 1
    assert events[0].body == "boom"
    assert events[0].service == "maestro"
    assert events[0].pipeline_id == "01AAA"
    assert events[0].timestamp is not None and events[0].timestamp.startswith("2024")


def test_read_otel_errors_missing_dir(tmp_path: Path) -> None:
    assert read_otel_errors(tmp_path / "no-logs") == []


def test_read_otel_errors_masks_secrets_in_body(tmp_path: Path) -> None:
    run = tmp_path / "logs" / "01CCCCCCCCCCCCCCCCCCCCCCCC"
    run.mkdir(parents=True)
    rec = {
        "SeverityNumber": 17,
        "SeverityText": "ERROR",
        "Body": (
            "conn to nats://admin:hunter2@host:4222 failed, "
            "auth Bearer sk-live-abc123456"
        ),
        "Timestamp": "1719999999000000000",
        "Resource": {"service.name": "svc"},
    }
    (run / "svc-1.jsonl").write_text(json.dumps(rec) + "\n")
    events = read_otel_errors(tmp_path / "logs")
    assert len(events) == 1
    assert "hunter2" not in events[0].body
    assert "sk-live-abc123456" not in events[0].body


def test_coerce_str() -> None:
    assert coerce_str(None) == "unknown"
    assert coerce_str(None, default="n/a") == "n/a"
    assert coerce_str(42) == "42"
    assert coerce_str("ok") == "ok"


def test_newest_mtime(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert newest_mtime([f, tmp_path / "missing"]) is not None
    assert newest_mtime([tmp_path / "missing"]) is None
