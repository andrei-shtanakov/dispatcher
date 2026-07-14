"""Tracked/ignored sync repos — dispatcher-owned sidecar state (DESIGN-205).

Decisions («отслеживать / не отслеживать») persist in a small TOML file next
to dispatcher.toml (default `dispatcher-sync.toml`). A sidecar, not the user's
config: dispatcher rewrites this file wholesale, and programmatically
rewriting a hand-edited dispatcher.toml would clobber comments and formatting.
Zero-docs bootstrap (brief FR-02 acceptance): the first sync run seeds the
file with every repo already present, so only genuinely new clones surface
as proposals afterwards.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

TrackAction = Literal["track", "ignore"]


class TrackingState(BaseModel):
    """The two decision sets; anything outside both is a proposal."""

    tracked: set[str] = Field(default_factory=set)
    ignored: set[str] = Field(default_factory=set)

    def known(self) -> set[str]:
        return self.tracked | self.ignored


def load_tracking(path: Path) -> TrackingState | None:
    """Read the sidecar; None means «not initialized yet» (triggers seeding)."""
    if not path.is_file():
        return None
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return TrackingState(
        tracked=set(data.get("tracked", [])),
        ignored=set(data.get("ignored", [])),
    )


def save_tracking(path: Path, state: TrackingState) -> None:
    """Rewrite the sidecar (dispatcher owns it wholesale)."""

    def _array(items: set[str]) -> str:
        if not items:
            return "[]"
        # json.dumps of each item is a valid TOML basic string
        body = "".join(f"  {json.dumps(item)},\n" for item in sorted(items))
        return "[\n" + body + "]"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# dispatcher-owned sync tracking state — edited via the Sync screen\n"
        "# (confirm/reject) or `POST /api/sync/track`; safe to hand-edit too.\n"
        f"tracked = {_array(state.tracked)}\n"
        f"ignored = {_array(state.ignored)}\n",
        encoding="utf-8",
    )


def seed_tracking(path: Path, dirs: set[str]) -> TrackingState:
    """First-run bootstrap: everything already present is tracked."""
    state = TrackingState(tracked=set(dirs))
    save_tracking(path, state)
    return state


def decide(path: Path, repo_dir: str, action: TrackAction) -> TrackingState:
    """Record one confirm/reject decision and persist it."""
    state = load_tracking(path) or TrackingState()
    if action == "track":
        state.tracked.add(repo_dir)
        state.ignored.discard(repo_dir)
    else:
        state.ignored.add(repo_dir)
        state.tracked.discard(repo_dir)
    save_tracking(path, state)
    return state
