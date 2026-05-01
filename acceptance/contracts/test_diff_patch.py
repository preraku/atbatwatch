"""diffPatch parser contract tests.

Drives the `atbatwatch parse-diff-patch` CLI against all captured fixtures and
asserts the output tuple shape.  No atbatwatch imports — we only call the
installed CLI as a subprocess.
"""

import json
import subprocess
from pathlib import Path

import pytest

_FIXTURES_ROOT = Path(__file__).parent.parent.parent / "fixtures" / "diff_patch"


def _run_parser(patch_path: Path, start_timecode: str) -> dict:
    """Run `atbatwatch parse-diff-patch` and return the parsed JSON result."""
    result = subprocess.run(
        [
            "uv",
            "run",
            "atbatwatch",
            "parse-diff-patch",
            str(patch_path),
            "--start-timecode",
            start_timecode,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"parse-diff-patch exited {result.returncode}:\n{result.stderr}"
    )
    return json.loads(result.stdout.strip())


# ---------------------------------------------------------------------------
# Parameterized cases derived from actual CLI output (verified against running stack)
# ---------------------------------------------------------------------------

# Each tuple: (game_pk, patch_file, start_timecode, expected_shape)
# expected_shape:
#   "needs_full_fetch" — null full_response, new_timecode == start_timecode, needs_full_fetch=True
#   "full_update"      — non-null full_response, new_timecode from body.metaData.timeStamp
#   "patch_no_ops"     — null full_response, new_timecode == start_timecode (empty op list)
#   "patch_with_ts"    — null full_response, new_timecode != start_timecode (ts op present)

_CASES = [
    # 823717 — two patches contain offense ops
    ("823717", "patch_t+15s.json", "20260429_191321", "needs_full_fetch"),
    ("823717", "patch_t+60s.json", "20260429_191321", "needs_full_fetch"),
    ("823717", "patch_t2+30s.json", "20260429_191458", "full_update"),
    # 824445 — empty patch, full_update, and plain patch_array
    ("824445", "patch_t+15s.json", "20260429_191400", "patch_no_ops"),
    ("824445", "patch_t+60s.json", "20260429_191400", "full_update"),
    ("824445", "patch_t2+30s.json", "20260429_191518", "patch_with_ts"),
    # 824608 — three plain patch_arrays with metadata timestamp ops
    ("824608", "patch_t+15s.json", "20260429_191323", "patch_with_ts"),
    ("824608", "patch_t+60s.json", "20260429_191323", "patch_with_ts"),
    ("824608", "patch_t2+30s.json", "20260429_191510", "patch_with_ts"),
]


@pytest.mark.parametrize(
    "game_pk,patch_file,start_timecode,shape",
    _CASES,
    ids=[f"{g}/{p}" for g, p, _, _ in _CASES],
)
def test_parse_diff_patch(game_pk, patch_file, start_timecode, shape):
    patch_path = _FIXTURES_ROOT / game_pk / patch_file
    assert patch_path.exists(), f"fixture not found: {patch_path}"

    out = _run_parser(patch_path, start_timecode)

    assert "full_response" in out
    assert "new_timecode" in out

    if shape == "needs_full_fetch":
        assert out["full_response"] is None
        assert out.get("needs_full_fetch") is True
        assert out["new_timecode"] == start_timecode

    elif shape == "full_update":
        assert out["full_response"] is not None, (
            "full_update should have non-null full_response"
        )
        assert out.get("needs_full_fetch") is not True
        # new_timecode must equal body.metaData.timeStamp
        body = out["full_response"]
        expected_ts = body.get("metaData", {}).get("timeStamp", "")
        assert out["new_timecode"] == expected_ts, (
            f"new_timecode {out['new_timecode']!r} != body.metaData.timeStamp {expected_ts!r}"
        )

    elif shape == "patch_no_ops":
        assert out["full_response"] is None
        assert out.get("needs_full_fetch") is not True
        assert out["new_timecode"] == start_timecode

    elif shape == "patch_with_ts":
        assert out["full_response"] is None
        assert out.get("needs_full_fetch") is not True
        # new_timecode must differ from start_timecode (a /metaData/timeStamp op was present)
        assert out["new_timecode"] != start_timecode, (
            f"expected new_timecode to advance from {start_timecode!r}"
        )
