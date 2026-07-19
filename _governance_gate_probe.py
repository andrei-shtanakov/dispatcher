"""TEMP governance-gate enforcement probe — DO NOT MERGE.

Deliberately introduces a real runtime resolve of a `_cowork_output/` path so the
GOV-003 gate (no-cowork-in-runtime) fails. Purpose: confirm the required
`governance / gate` status check both goes red AND blocks the merge on dispatcher.
Delete this file / close the PR once enforcement is confirmed.
"""

from pathlib import Path

BAD = Path("_cowork_output/roadmap/latest.json")  # runtime path resolve — GOV-003 must flag
