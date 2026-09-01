"""Archive volume grouping and cleanup of known source paths."""
import re
from pathlib import Path
from typing import Iterable
from postprocessors.archive.extractor import archive_paths_from_downloads

_PART_RAR_RE = re.compile(r"^(?P<base>.+)\.part(?P<part>\d+)\.rar$", re.IGNORECASE)
_OLD_RAR_RE = re.compile(r"^(?P<base>.+)\.r(?P<part>\d{2})$", re.IGNORECASE)


def _normalise_paths(paths: Iterable[str | Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        if not raw:
            continue
        path = Path(raw)
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _canonical_archive_entries(paths: Iterable[str | Path]) -> list[Path]:
    """Return one extraction entry point per DB-known archive set."""
    known = _normalise_paths(paths)
    entries = archive_paths_from_downloads(known)

    # Traditional split RAR sets are named payload.rar, payload.r00, payload.r01...
    # The .rar file is the canonical entry when it exists; 7-Zip consumes the
    # numbered companions automatically. Keep .r00 as an entry only for sets
    # where no matching .rar was downloaded.
    traditional_roots = {
        (str(path.parent), path.name[:-4].casefold())
        for path in known
        if path.suffix.casefold() == ".rar" and not _PART_RAR_RE.fullmatch(path.name)
    }

    canonical: list[Path] = []
    for entry in entries:
        old_part = _OLD_RAR_RE.fullmatch(entry.name)
        if old_part and (
            str(entry.parent),
            old_part.group("base").casefold(),
        ) in traditional_roots:
            continue
        canonical.append(entry)
    return canonical


def _archive_source_paths(entry: Path, known_paths: Iterable[str | Path]) -> list[Path]:
    """Return only DB-known source volumes belonging to *entry*'s archive set."""
    entry = Path(entry)
    known = _normalise_paths(known_paths)

    part_match = _PART_RAR_RE.fullmatch(entry.name)
    if part_match:
        base = part_match.group("base").casefold()
        members: list[tuple[int, Path]] = []
        for path in known:
            match = _PART_RAR_RE.fullmatch(path.name)
            if (
                path.parent == entry.parent
                and match
                and match.group("base").casefold() == base
            ):
                members.append((int(match.group("part")), path))
        if members:
            return [path for _part, path in sorted(members, key=lambda item: item[0])]
        return [entry]

    old_match = _OLD_RAR_RE.fullmatch(entry.name)
    if old_match:
        base = old_match.group("base")
    elif entry.suffix.casefold() == ".rar":
        base = entry.name[:-4]
        has_numbered_companion = any(
            path.parent == entry.parent
            and (match := _OLD_RAR_RE.fullmatch(path.name)) is not None
            and match.group("base").casefold() == base.casefold()
            for path in known
        )
        if not has_numbered_companion:
            return [entry]
    else:
        return [entry]

    base_folded = base.casefold()
    root_name = f"{base}.rar".casefold()
    root: Path | None = None
    numbered: list[tuple[int, Path]] = []
    for path in known:
        if path.parent != entry.parent:
            continue
        if path.name.casefold() == root_name:
            root = path
            continue
        match = _OLD_RAR_RE.fullmatch(path.name)
        if match and match.group("base").casefold() == base_folded:
            numbered.append((int(match.group("part")), path))

    sources: list[Path] = []
    if root is not None:
        sources.append(root)
    sources.extend(path for _part, path in sorted(numbered, key=lambda item: item[0]))
    return sources or [entry]


def _cleanup_successful_sources(
    successful_entries: Iterable[Path],
    source_paths_by_entry: dict[str, list[Path]],
    existed_before: set[str],
) -> tuple[int, int, list[tuple[Path, str]]]:
    """Remove DB-owned source volumes for successfully extracted archive sets."""
    targets: list[Path] = []
    seen: set[str] = set()
    for entry in successful_entries:
        for path in source_paths_by_entry.get(str(entry), [Path(entry)]):
            key = str(path)
            if key in seen or key not in existed_before:
                continue
            seen.add(key)
            targets.append(path)

    removed = 0
    failures: list[tuple[Path, str]] = []
    for path in targets:
        if not path.exists():
            # The extractor owns deletion of its entry volume. Count that as part
            # of the same requested cleanup operation rather than deleting twice.
            removed += 1
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            failures.append((path, str(exc)))
    return removed, len(targets), failures

