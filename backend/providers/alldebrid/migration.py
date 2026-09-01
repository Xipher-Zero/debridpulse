"""One-time decoding of AllDebrid fields from the v1 database format."""
from urllib.parse import urlsplit

from providers.alldebrid.translation import observation_from_native, resource_from_native
from transfers.models import Endpoint, Ownership, SourceIdentity, TransferCandidate, TransferRequest


def legacy_resource(row):
    identity = str(row.get("alldebrid_id") or "").strip()
    if not identity:
        return None
    ownership = Ownership.CREATED if row.get("source") in {"manual", "manual_file", "api"} else Ownership.OBSERVED
    resource = resource_from_native({"id": identity}, ownership=ownership)
    return observation_from_native({"id": identity, "statusCode": row.get("provider_status_code") or 0,
                                    "status": row.get("provider_status") or "", "name": row.get("name") or "",
                                    "hash": row.get("hash") or ""}, resource=resource)


def legacy_candidate(row, resource=None):
    address = str(row.get("download_url") or "")
    source = str(row.get("source_url") or "")
    if not address:
        return None
    request = TransferRequest(urlsplit(source).scheme, source, name=str(row.get("filename") or ""), preferred_provider="alldebrid") if source else None
    return TransferCandidate(str(row.get("filename") or "download"), (Endpoint(urlsplit(address).scheme, address),),
                             expected_bytes=max(0, int(row.get("size_bytes") or 0)), provider_id="alldebrid", resource=resource,
                             refresh_request=request, id=f"v1-file-{row['id']}",
                             source_identity=SourceIdentity("host", str(urlsplit(source).hostname or "").casefold().removeprefix("www.").rstrip(".")))
