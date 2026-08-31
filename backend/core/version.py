"""Single authoritative DebridPulse version source and release ordering."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re


_VERSION_RE = re.compile(
    r"^(?P<release>\d+(?:\.\d+)*)"
    r"(?:(?:[-_.]?)(?P<pre>a|alpha|b|beta|rc|pre|preview)(?P<pre_n>\d+))?$",
    re.IGNORECASE,
)
_PRE_RANK = {
    "a": 0,
    "alpha": 0,
    "b": 1,
    "beta": 1,
    "rc": 2,
    "pre": 2,
    "preview": 2,
}
_FINAL_RANK = 3


@dataclass(frozen=True)
class ParsedVersion:
    release: tuple[int, ...]
    stage_rank: int
    stage_number: int


def normalize_version_tag(value: str) -> str:
    tag = str(value or "").strip()
    if tag.startswith("internal-v"):
        return tag[len("internal-v"):]
    return tag.lstrip("v")


def parse_version(value: str) -> ParsedVersion | None:
    """Parse the release forms DebridPulse publishes, including RC tags.

    Unknown/non-release text intentionally returns ``None`` so callers fail
    closed instead of treating malformed input as version zero.
    """
    normalized = normalize_version_tag(value)
    match = _VERSION_RE.fullmatch(normalized)
    if match is None:
        return None
    release = tuple(int(part) for part in match.group("release").split("."))
    pre = (match.group("pre") or "").lower()
    if not pre:
        return ParsedVersion(release, _FINAL_RANK, 0)
    return ParsedVersion(
        release,
        _PRE_RANK[pre],
        int(match.group("pre_n") or 0),
    )


def compare_versions(left: str, right: str) -> int | None:
    """Return -1/0/1 for two valid release versions, otherwise ``None``."""
    a = parse_version(left)
    b = parse_version(right)
    if a is None or b is None:
        return None

    width = max(len(a.release), len(b.release))
    a_release = a.release + (0,) * (width - len(a.release))
    b_release = b.release + (0,) * (width - len(b.release))
    if a_release < b_release:
        return -1
    if a_release > b_release:
        return 1

    a_stage = (a.stage_rank, a.stage_number)
    b_stage = (b.stage_rank, b.stage_number)
    if a_stage < b_stage:
        return -1
    if a_stage > b_stage:
        return 1
    return 0


def is_version_newer(candidate: str, current: str) -> bool:
    comparison = compare_versions(candidate, current)
    return comparison is not None and comparison > 0


@lru_cache(maxsize=1)
def read_version() -> str:
    for candidate in (
        Path(__file__).resolve().parents[2] / "VERSION",
        Path("/app/VERSION"),
    ):
        try:
            value = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return "unknown"
