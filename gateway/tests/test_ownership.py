"""Tests for #177 Phase A ownership lock."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from stackchan_mcp.ownership import (
    OwnershipError,
    acquire_lock,
    generate_owner_id,
    is_pid_alive,
    read_lock,
    release_lock,
)


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "owner.lock"


def test_acquire_when_no_lock_succeeds(lock_path: Path) -> None:
    info = acquire_lock("test-owner-1", lock_path)
    assert info["owner_id"] == "test-owner-1"
    assert info["pid"] == os.getpid()
    assert lock_path.exists()
    data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert data["owner_id"] == "test-owner-1"


def test_acquire_when_live_lock_refuses(lock_path: Path) -> None:
    acquire_lock("first-owner", lock_path)
    with pytest.raises(OwnershipError, match="already owned by first-owner"):
        acquire_lock("second-owner", lock_path)


def test_acquire_when_stale_lock_overwrites(lock_path: Path) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "owner_id": "dead-owner",
                "pid": 999999,
                "start_ts": "2000-01-01T00:00:00Z",
                "host": "nowhere",
            }
        ),
        encoding="utf-8",
    )
    info = acquire_lock("new-owner", lock_path)
    assert info["owner_id"] == "new-owner"
    data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert data["owner_id"] == "new-owner"


def test_release_is_idempotent(lock_path: Path) -> None:
    acquire_lock("owner", lock_path)
    release_lock(lock_path)
    assert not lock_path.exists()
    release_lock(lock_path)


def test_read_lock_returns_none_when_missing(lock_path: Path) -> None:
    assert read_lock(lock_path) is None


def test_is_pid_alive_for_self_returns_true() -> None:
    assert is_pid_alive(os.getpid()) is True


def test_is_pid_alive_for_dead_pid_returns_false() -> None:
    assert is_pid_alive(999999) is False


def test_generate_owner_id_uses_env_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STACKCHAN_OWNER_ID", "custom-id")
    assert generate_owner_id() == "custom-id"


def test_generate_owner_id_falls_back_to_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STACKCHAN_OWNER_ID", raising=False)
    label = generate_owner_id()
    assert label.startswith("stackchan-mcp-")
    assert len(label) == len("stackchan-mcp-") + 8
