"""Archive execution returns facts; the transfer core owns lifecycle state."""
from pathlib import Path

from core.config import get_settings
from postprocessors.archive.secure import get_secure_extractor
from postprocessors.archive.sources import _archive_source_paths, _canonical_archive_entries, _cleanup_successful_sources
from transfers.errors import Category, Domain, NormalizedError, Retryability, Stage, safe_diagnostic
from transfers.models import IntegrationDescriptor, OutcomeKind, TransferOutcome


class ArchivePostProcessor:
    descriptor = IntegrationDescriptor("archive", "Archive extraction", frozenset())

    async def process(self, transfer_id, paths):
        cfg = get_settings()
        archives = _canonical_archive_entries(paths)
        if not archives:
            return TransferOutcome(OutcomeKind.SKIPPED, detail="No supported archives")
        source_paths = {str(archive): _archive_source_paths(archive, paths) for archive in archives}
        existed = {str(path) for sources in source_paths.values() for path in sources if path.exists()}
        extractor = get_secure_extractor()
        extractor.update_max_concurrent(max(1, cfg.extract_max_concurrent))
        # Clean up only confirmed successes and only the core-supplied paths.
        results = await extractor.extract_archives(archives, delete_after=False)
        successes = [Path(path) for path, ok, _message in results if ok]
        failures = [str(message) for _path, ok, message in results if not ok]
        if len(results) != len(archives):
            failures.append("Extraction did not return a result for every archive")
        if cfg.extract_delete_archive and successes:
            _removed, _total, cleanup_errors = _cleanup_successful_sources(successes, source_paths, existed)
            failures.extend(message for _path, message in cleanup_errors)
        if failures:
            return TransferOutcome(OutcomeKind.FAILURE, NormalizedError(
                Domain.POST_PROCESSING, Category.POST_PROCESSING_FAILED, Stage.POST_PROCESSING,
                retryability=Retryability.UNKNOWN, integration_id=self.descriptor.id,
                diagnostic=safe_diagnostic("; ".join(failures))))
        return TransferOutcome(OutcomeKind.SUCCESS, detail=f"Extracted {len(successes)} archive(s)")
