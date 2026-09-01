/* Canonical error categories supplied by the transfer API. No native-code or text parsing. */
(function () {
  'use strict';
  const labels = Object.freeze({
    invalid_request: 'Invalid Request', unsupported_request: 'Unsupported Request',
    unsupported_capability: 'Unsupported Operation', invalid_configuration: 'Configuration Required',
    authentication_failed: 'Authentication Failed', authorization_failed: 'Access Denied',
    credential_missing: 'Credentials Required', credential_expired: 'Credentials Expired', credential_invalid: 'Invalid Credentials',
    source_not_found: 'Source Missing', source_unavailable: 'Source Unavailable', source_temporarily_unavailable: 'Source Unavailable', source_expired: 'Source Expired',
    provider_unavailable: 'Provider Unavailable', provider_degraded: 'Provider Degraded', provider_maintenance: 'Provider Maintenance',
    resource_not_found: 'Resource Missing', resource_expired: 'Resource Expired',
    rate_limited: 'Rate Limited', quota_exceeded: 'Quota Exceeded', concurrency_limited: 'Concurrency Limited', account_limited: 'Account Limited', resource_exhausted: 'Resource Exhausted',
    resolution_failed: 'Resolution Failed', resolution_temporarily_failed: 'Resolution Unavailable', no_transfer_candidate: 'No Download Available',
    candidate_expired: 'Download Link Expired', candidate_rejected: 'Download Rejected',
    dns_failure: 'DNS Failure', connection_failed: 'Connection Failed', connection_timeout: 'Connection Timeout', read_timeout: 'Read Timeout', remote_reset: 'Connection Reset',
    protocol_error: 'Protocol Error', tls_failure: 'TLS Failure', host_key_failure: 'Host Identity Failed',
    destination_blocked: 'Destination Blocked', egress_policy_violation: 'Connection Blocked', unsafe_redirect: 'Redirect Blocked', tls_identity_failure: 'TLS Identity Failed', path_policy_violation: 'Path Blocked', security_policy_rejected: 'Security Policy Rejected',
    executor_unavailable: 'Downloader Unavailable', executor_rejected: 'Download Rejected', transfer_failed: 'Download Failed', transfer_stalled: 'Download Stalled', transfer_interrupted: 'Download Interrupted',
    remote_read_failed: 'Remote Read Failed', remote_write_failed: 'Remote Write Failed', size_mismatch: 'Size Mismatch', checksum_mismatch: 'Checksum Mismatch', content_invalid: 'Invalid Content', materialization_failed: 'Local Verification Failed',
    local_path_conflict: 'Path Conflict', disk_full: 'Disk Full', permission_denied: 'Permission Denied', local_io_failure: 'Local I/O Failed', path_unavailable: 'Path Unavailable', local_resource_exhausted: 'Local Resource Exhausted',
    ownership_conflict: 'Ownership Conflict', resource_state_conflict: 'Resource State Conflict', reconciliation_failed: 'Reconciliation Failed', orphaned_resource: 'Execution Missing', recovery_failed: 'Recovery Requires Attention', state_inconsistent: 'State Requires Attention',
    remote_cleanup_failed: 'Remote Cleanup Failed', local_cleanup_failed: 'Local Cleanup Failed', post_processing_failed: 'Post-processing Failed', extraction_failed: 'Extraction Failed',
    provider_protocol_violation: 'Provider Protocol Error', executor_protocol_violation: 'Downloader Protocol Error', invalid_adapter_response: 'Integration Protocol Error',
    unmapped_provider_error: 'Provider Requires Attention', unmapped_executor_error: 'Downloader Requires Attention', internal_error: 'Requires Attention'
  });
  function classify(detail) {
    const category = detail && detail.error && detail.error.category;
    return typeof category === 'string' && Object.prototype.hasOwnProperty.call(labels, category) ? category : 'internal_error';
  }
  window.DPFailureSemantics = Object.freeze({labels: labels, classify: classify});
})();
