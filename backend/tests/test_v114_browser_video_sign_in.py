"""1.0.14 (operator, 2026-11-19): a connected browser's per-job video sign-in.

The worker half, against the pinned yt-dlp's own cookie jar and no network:
the instruction's cookies are sent only where the browser itself would send
them, nothing is read from or written to disk, and a failure detail never
carries a value. The server half (what a browser may send, and that it is
never part of a job) is ``test_browser_jobs``; the holder's hand-out rules
through the real engine are ``test_v114_browser_cookie_holder``.
"""
from __future__ import annotations

from yt_dlp import YoutubeDL

from executors.media import worker

VALUE = "v4lue-only-the-video-site-sees"


def _spec(**cookie):
    return {"cookies": [{"name": "SID", "value": VALUE, "domain": ".video.test", "path": "/", "secure": True,
                         "http_only": True, "host_only": False, "expires": None, **cookie}]}


def _sent(spec, url):
    with YoutubeDL({"cookiefile": None, "cookiesfrombrowser": None, "quiet": True}) as ydl:
        worker._install_cookies(ydl, spec)
        return ydl.cookiejar.get_cookie_header(url) or ""


def test_a_cookie_goes_only_where_the_browser_would_send_it():
    assert _sent(_spec(), "https://www.video.test/watch?v=1") == f"SID={VALUE}"
    assert _sent(_spec(), "https://video.test/") == f"SID={VALUE}"
    assert _sent(_spec(), "http://www.video.test/") == ""          # secure: never over plain HTTP
    assert _sent(_spec(), "https://video.test.evil.example/") == ""
    assert _sent(_spec(), "https://cdn.other.test/") == ""
    host_only = _spec(domain="www.video.test", host_only=True)
    assert _sent(host_only, "https://www.video.test/") == f"SID={VALUE}"
    assert _sent(host_only, "https://sub.www.video.test/") == ""
    assert _sent(_spec(path="/members"), "https://www.video.test/watch") == ""
    assert _sent({}, "https://www.video.test/") == ""


def test_no_cookie_source_but_the_instruction_and_no_value_in_a_detail():
    params = worker._params({"tools": {}, "proxy": "http://127.0.0.1:9"}, worker._Log())
    assert params["cookiefile"] is None and params["cookiesfrombrowser"] is None
    _sent(_spec(), "https://www.video.test/")
    assert VALUE not in worker._detail(f"HTTP Error 403 for SID={VALUE}")
