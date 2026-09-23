"""1.0.13 Gate-9 rev-5, item 3: secrets never travel in a URL.

SABnzbd 5.1.3 accepts every control mode as an ordinary form-encoded POST body
(characterized against the bundled 5.1.3 service: version, queue, history,
get_config, fullstatus, set_config for both `misc` and a credentialed
`servers` keyword, del_config, and test_server all answer HTTP 200 with the
whole request in the body and an EMPTY query string).

So there is no reason for the internal API key or an operator's NNTP password
to sit in a request line, where it reaches access logs, proxies, process
listings, exception text and crash diagnostics. This module drives the real
client with sentinel secrets and asserts they appear in no URL, no log record
and no surfaced exception.
"""
from __future__ import annotations

import logging

import pytest

from executors.sabnzbd.client import SabEndpoint, SabnzbdClient

API_KEY_SENTINEL = "SENTINELAPIKEY0123456789"
PASSWORD_SENTINEL = "SENTINELpassw0rd!$#"
USERNAME_SENTINEL = "SENTINELuser"
SENTINELS = (API_KEY_SENTINEL, PASSWORD_SENTINEL, USERNAME_SENTINEL)


class _Response:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class RecordingSession:
    """Records every request line and body the client produces."""

    calls: list = []

    def __init__(self, status=200, body=None):
        body = body if body is not None else EVERY_SHAPE
        self._status, self._body = status, body

    def get(self, url):
        RecordingSession.calls.append(("GET", url, None))
        return _Response(self._status, self._body)

    def post(self, url, data=None, headers=None):
        RecordingSession.calls.append(("POST", url, data))
        return _Response(self._status, self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


# One body that satisfies every response shape the client validates, so a
# probe can drive all of them without scripting each call.
EVERY_SHAPE = (
    '{"status": true, "version": "5.1.3", "nzo_ids": ["SABnzbd_nzo_1"],'
    ' "queue": {"slots": [], "noofslots": 0},'
    ' "history": {"slots": [], "noofslots": 0},'
    ' "config": {"servers": [], "misc": {}},'
    ' "value": {"result": true, "message": "ok"}}'
)


def client(body=EVERY_SHAPE):
    RecordingSession.calls = []
    endpoint = SabEndpoint("http://127.0.0.1:8090", API_KEY_SENTINEL, 5)
    return SabnzbdClient(endpoint, session_factory=lambda: RecordingSession(200, body))


def urls():
    return [url for _, url, _ in RecordingSession.calls]


def body_text():
    """Every recorded request body flattened to decoded text.

    A form body is percent-encoded, so the raw bytes must be decoded before
    looking for a sentinel -- otherwise "absent" and "merely escaped" are
    indistinguishable.
    """
    from urllib.parse import unquote_plus

    out = []
    for _, _, data in RecordingSession.calls:
        if data is None:
            continue
        raw = data.decode() if isinstance(data, bytes) else str(getattr(data, "_fields", data))
        out.append(unquote_plus(raw))
    return "\n".join(out)


# --- no secret may appear in any request line -----------------------------

@pytest.mark.asyncio
async def test_the_api_key_never_appears_in_a_request_url():
    sab = client()
    await sab.version()
    await sab.queue_snapshot()
    await sab.history_snapshot()
    await sab.get_config("servers")
    assert urls(), "the probe must actually have issued requests"
    for url in urls():
        assert API_KEY_SENTINEL not in url, f"internal API key leaked into a URL: {url}"


@pytest.mark.asyncio
async def test_a_news_server_credential_never_appears_in_a_request_url():
    sab = client()
    await sab.set_config("servers", "dp-abc", host="news.example.net", port=563, ssl=1,
                         username=USERNAME_SENTINEL, password=PASSWORD_SENTINEL,
                         connections=8, priority=0, enable=1, displayname="news")
    for url in urls():
        for sentinel in SENTINELS:
            assert sentinel not in url, f"{sentinel!r} leaked into a URL: {url}"


@pytest.mark.asyncio
async def test_test_server_credentials_never_appear_in_a_request_url():
    sab = client()
    await sab.test_server(host="news.example.net", port=563, ssl=1,
                          username=USERNAME_SENTINEL, password=PASSWORD_SENTINEL, connections=8)
    for url in urls():
        for sentinel in SENTINELS:
            assert sentinel not in url, f"{sentinel!r} leaked into a URL: {url}"


@pytest.mark.asyncio
async def test_control_calls_have_no_query_string_at_all():
    """Not "no secrets in the query string" -- no query string."""
    sab = client()
    await sab.version()
    await sab.queue_snapshot()
    await sab.set_config("misc", "download_dir", value="/download/.dpwork/incomplete")
    for _, url, _ in RecordingSession.calls:
        assert "?" not in url, f"control call still carries a query string: {url}"


@pytest.mark.asyncio
async def test_the_credential_actually_travels_in_the_body():
    """The secret has to go somewhere: prove it moved rather than vanished."""
    sab = client()
    await sab.set_config("servers", "dp-abc", host="news.example.net", port=563, ssl=1,
                         username=USERNAME_SENTINEL, password=PASSWORD_SENTINEL,
                         connections=8, priority=0, enable=1)
    text = body_text()
    assert PASSWORD_SENTINEL in text and API_KEY_SENTINEL in text, text[:400]


@pytest.mark.asyncio
async def test_addfile_still_submits_its_payload_in_a_multipart_body():
    sab = client()
    await sab.addfile(b"<nzb/>", nzbname="dp-token")
    method, url, data = RecordingSession.calls[-1]
    assert method == "POST" and "?" not in url
    assert API_KEY_SENTINEL not in url


# --- no secret may reach a log record or a surfaced exception -------------

@pytest.mark.asyncio
async def test_no_secret_reaches_a_log_record(caplog):
    sab = client()
    with caplog.at_level(logging.DEBUG):
        await sab.version()
        await sab.set_config("servers", "dp-abc", host="news.example.net", port=563, ssl=1,
                             username=USERNAME_SENTINEL, password=PASSWORD_SENTINEL,
                             connections=8, priority=0, enable=1)
    recorded = "\n".join(record.getMessage() for record in caplog.records)
    for sentinel in SENTINELS:
        assert sentinel not in recorded, f"{sentinel!r} reached the log"


@pytest.mark.asyncio
async def test_no_secret_reaches_a_transport_exception():
    class Failing(RecordingSession):
        def get(self, url):
            RecordingSession.calls.append(("GET", url, None))
            raise OSError(f"connect failed for {url}")

        def post(self, url, data=None, headers=None):
            RecordingSession.calls.append(("POST", url, data))
            raise OSError(f"connect failed for {url} body={data}")

    RecordingSession.calls = []
    endpoint = SabEndpoint("http://127.0.0.1:8090", API_KEY_SENTINEL, 5)
    sab = SabnzbdClient(endpoint, session_factory=lambda: Failing())
    with pytest.raises(Exception) as caught:
        await sab.set_config("servers", "dp-abc", host="news.example.net", port=563, ssl=1,
                             username=USERNAME_SENTINEL, password=PASSWORD_SENTINEL,
                             connections=8, priority=0, enable=1)
    text = f"{caught.value}\n{caught.value.__cause__}"
    for sentinel in SENTINELS:
        assert sentinel not in text, f"{sentinel!r} surfaced in an exception: {text[:300]}"


def test_the_client_declares_its_api_key_as_a_secret():
    """So every executor-side sanitizer already knows to redact it."""
    sab = client()
    assert API_KEY_SENTINEL in sab.secrets


# --- the native service's own output must not bypass the boundary ---------

def test_native_service_output_is_not_logged_verbatim():
    import inspect
    import textwrap

    from executors.sabnzbd import runtime

    source = textwrap.dedent(inspect.getsource(runtime.UsenetRuntime._drain))
    body = source.split('"""')[-1] if '"""' in source else source
    assert "line.decode" not in body or "sanitize" in body or "redact" in body, (
        "native stdout/stderr must be sanitized or suppressed, never forwarded verbatim"
    )


@pytest.mark.asyncio
async def test_a_native_test_message_never_echoes_the_operators_password():
    """SAB quotes parts of a failed request back; the credential must not ride
    along into the API response the operator sees."""
    from executors.sabnzbd.admin import SabnzbdAdministration
    from integrations.usenet.definition import UsenetOptions

    class Echoing:
        secrets = ()

        async def test_server(self, **kw):
            return False, f"rejected host={kw['host']} user={kw['username']} pass={kw['password']}"

    admin = SabnzbdAdministration(Echoing(), UsenetOptions(), "/download")
    result = await admin.test_server(host="news.example.net", port=563, ssl=True,
                                     username=USERNAME_SENTINEL, password=PASSWORD_SENTINEL,
                                     connections=8)
    assert PASSWORD_SENTINEL not in str(result), result
