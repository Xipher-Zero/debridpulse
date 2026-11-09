/* Canonical error categories supplied by the transfer API. No native-code or text parsing. */
(function () {
  'use strict';
  // The canonical failure categories; each one's visible name is the catalog's
  // error_category.<category> (ui-i18n.js), read when it is shown.
  const categories = new Set([
    'invalid_request', 'unsupported_request', 'unsupported_capability', 'invalid_configuration',
    'authentication_failed', 'authorization_failed', 'credential_missing', 'credential_expired',
    'credential_invalid', 'source_not_found', 'source_unavailable', 'source_temporarily_unavailable',
    'source_expired', 'provider_unavailable', 'provider_degraded', 'provider_maintenance', 'resource_not_found',
    'resource_expired', 'rate_limited', 'quota_exceeded', 'concurrency_limited', 'account_limited',
    'resource_exhausted', 'resolution_failed', 'resolution_temporarily_failed', 'no_transfer_candidate',
    'candidate_expired', 'candidate_rejected', 'dns_failure', 'connection_failed', 'connection_refused',
    'connection_timeout', 'read_timeout', 'remote_reset', 'protocol_error', 'tls_failure', 'host_key_failure',
    'destination_blocked', 'egress_policy_violation', 'unsafe_redirect', 'tls_identity_failure',
    'path_policy_violation', 'security_policy_rejected', 'executor_unavailable', 'executor_rejected',
    'transfer_failed', 'transfer_stalled', 'transfer_interrupted', 'remote_read_failed', 'remote_write_failed',
    'size_mismatch', 'checksum_mismatch', 'content_invalid', 'materialization_failed', 'local_path_conflict',
    'disk_full', 'permission_denied', 'local_io_failure', 'path_unavailable', 'local_resource_exhausted',
    'ownership_conflict', 'resource_state_conflict', 'reconciliation_failed', 'orphaned_resource', 'recovery_failed',
    'state_inconsistent', 'remote_cleanup_failed', 'local_cleanup_failed', 'post_processing_failed',
    'extraction_failed', 'provider_protocol_violation', 'executor_protocol_violation', 'invalid_adapter_response',
    'unmapped_provider_error', 'unmapped_executor_error', 'internal_error'
  ]);
  function classify(detail) {
    const category = detail && detail.error && detail.error.category;
    return typeof category === 'string' && categories.has(category) ? category : 'internal_error';
  }
  function label(category) {
    return window.DPI18n.t(`error_category.${categories.has(category) ? category : 'internal_error'}`);
  }
  window.DPFailureSemantics = Object.freeze({label: label, classify: classify});
})();
