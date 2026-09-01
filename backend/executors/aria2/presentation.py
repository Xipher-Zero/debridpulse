"""Explicit aria2 administration view; transfer views use canonical models."""
from executors.aria2.client import aria2_download_to_dict
from executors.aria2.translation import native_failure


def public_aria2_download(download):
    payload = aria2_download_to_dict(download)
    payload["files"] = [
        {key: value for key, value in item.items() if key != "uris"}
        for item in payload.get("files", [])
    ]
    error = native_failure(download.error_code, download.error_message) if download.status == "error" else None
    payload.pop("error_code", None)
    payload["error_message"] = error.message if error else ""
    payload["error"] = error.as_dict() if error else None
    return payload
