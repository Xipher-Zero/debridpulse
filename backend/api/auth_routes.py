from __future__ import annotations

import base64
import hashlib
import html
import os
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from auth.csrf import (
    clear_login_csrf_cookie,
    login_csrf_cookie_name,
    login_csrf_store,
    set_login_csrf_cookie,
)
from auth.manager import (
    PasswordAuthenticationBusy,
    password_authentication_snapshot_current,
    peer_key,
    verify_local_credentials,
)
from auth.models import AuthMechanism, Principal
from auth.oidc import (
    OIDC_CORRELATION_COOKIE,
    OIDC_TRANSACTION_TTL_SECONDS,
    OidcError,
    begin_oidc_login,
    complete_oidc_login,
    oidc_auth_ready,
    oidc_transaction_store,
)
from auth.oidc_version import oidc_configuration_version
from auth.passwords import password_credential_version
from auth.policy import (
    interactive_auth_enabled,
    oidc_auth_enabled,
    password_auth_enabled,
    password_auth_ready,
    safe_return_path,
)
from auth.sessions import (
    clear_session_cookie,
    session_cookie_token,
    session_store,
    set_session_cookie,
)
from auth.throttle import login_challenge_rate_limiter, oidc_start_rate_limiter
from auth.transitions import authentication_configuration_lock
from core.config import get_settings
from core.version import read_version


router = APIRouter()


# The login page remains server-rendered and self-contained. The only executable
# code is this tiny hash-pinned interaction shim: it restores the user's stored
# palette and provides press-and-hold password reveal. It performs no network,
# cookie, authentication-state, or token operations.
_AUTH_PAGE_SCRIPT = """(()=>{\"use strict\";try{const t=localStorage.getItem(\"theme\");if(t===\"light\"||t===\"dark\")document.documentElement.dataset.theme=t}catch(_){}document.addEventListener(\"DOMContentLoaded\",()=>{const b=document.querySelector(\"[data-password-reveal]\"),p=document.getElementById(\"password\");if(!b||!p)return;const s=()=>{p.type=\"text\";b.setAttribute(\"aria-pressed\",\"true\");b.setAttribute(\"aria-label\",\"Release to hide password\")},h=()=>{p.type=\"password\";b.setAttribute(\"aria-pressed\",\"false\");b.setAttribute(\"aria-label\",\"Hold to show password\")};b.addEventListener(\"pointerdown\",a=>{if(a.pointerType===\"mouse\"&&a.button!==0)return;a.preventDefault();try{b.setPointerCapture(a.pointerId)}catch(_){}s()});b.addEventListener(\"pointerup\",a=>{h();try{b.releasePointerCapture(a.pointerId)}catch(_){}});b.addEventListener(\"pointercancel\",h);b.addEventListener(\"pointerleave\",()=>{if(b.getAttribute(\"aria-pressed\")==\"true\")h()});document.addEventListener(\"pointerup\",h,true);b.addEventListener(\"keydown\",a=>{if((a.key===\" \"||a.key===\"Enter\")&&!a.repeat){a.preventDefault();s()}});b.addEventListener(\"keyup\",a=>{if(a.key===\" \"||a.key===\"Enter\"){a.preventDefault();h()}});b.addEventListener(\"blur\",h);window.addEventListener(\"blur\",h);document.addEventListener(\"visibilitychange\",()=>{if(document.hidden)h()})})})();"""
_AUTH_PAGE_SCRIPT_HASH = base64.b64encode(
    hashlib.sha256(_AUTH_PAGE_SCRIPT.encode("utf-8")).digest()
).decode("ascii")


_AUTH_PAGE_STYLE = """
:root {
  color-scheme: dark;
  --bg:#040711;--bg2:#070c1a;
  --card:rgba(15,22,41,.76);--card2:rgba(7,12,27,.68);--card-edge:rgba(105,91,168,.58);
  --field:rgba(10,17,33,.76);--border:#26324d;--border-strong:#3a496a;
  --text:#f7f8ff;--text2:#b3bcda;--text3:#7c87aa;--accent:#b45cff;--accent2:#3d94ff;
  --accent-rgb:155,69,255;--blue-rgb:61,148,255;--danger:#ff5264;
  --icon-accent:#a99cff;--icon-accent-strong:#83b4ff;
  --primary-gradient:linear-gradient(100deg,#8c22ed 0%,#6c37f5 48%,#1688ff 100%);
  --primary-gradient-hover:linear-gradient(100deg,#a038ff 0%,#764cff 48%,#2497ff 100%);
  --wave-purple:#b632ff;--wave-blue:#168cff;--particle:rgba(125,139,255,.90);
  --shadow:0 28px 78px rgba(0,0,10,.42),0 10px 28px rgba(0,0,8,.24);--input-shadow:inset 0 1px 0 rgba(255,255,255,.028);
}
:root[data-theme="light"] {
  color-scheme: light;
  --bg:#f7f8fd;--bg2:#eef3fb;
  --card:rgba(255,255,255,.78);--card2:rgba(248,250,255,.68);--card-edge:rgba(133,111,198,.26);
  --field:rgba(255,255,255,.76);--border:#dce4f1;--border-strong:#c4cee0;
  --text:#111a34;--text2:#526184;--text3:#7b88a8;--accent:#9637f5;--accent2:#2f86ff;
  --accent-rgb:132,40,237;--blue-rgb:47,134,255;--danger:#e64255;
  --icon-accent:#806de8;--icon-accent-strong:#4b8ff5;
  --primary-gradient:linear-gradient(100deg,#8f25ee 0%,#6d42f4 48%,#1688ff 100%);
  --primary-gradient-hover:linear-gradient(100deg,#7d1ed6 0%,#5d36df 48%,#0875e9 100%);
  --wave-purple:#ad48fa;--wave-blue:#4f91ff;--particle:rgba(99,108,222,.64);
  --shadow:0 22px 56px rgba(45,61,96,.13),0 7px 20px rgba(45,61,96,.08);--input-shadow:inset 0 1px 0 rgba(255,255,255,.86);
}
@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]) {
    color-scheme: light;
    --bg:#f7f8fd;--bg2:#eef3fb;
    --card:rgba(255,255,255,.78);--card2:rgba(248,250,255,.68);--card-edge:rgba(133,111,198,.26);
    --field:rgba(255,255,255,.76);--border:#dce4f1;--border-strong:#c4cee0;
    --text:#111a34;--text2:#526184;--text3:#7b88a8;--accent:#9637f5;--accent2:#2f86ff;
    --accent-rgb:132,40,237;--blue-rgb:47,134,255;--danger:#e64255;
    --icon-accent:#806de8;--icon-accent-strong:#4b8ff5;
    --primary-gradient:linear-gradient(100deg,#8f25ee 0%,#6d42f4 48%,#1688ff 100%);
    --primary-gradient-hover:linear-gradient(100deg,#7d1ed6 0%,#5d36df 48%,#0875e9 100%);
    --wave-purple:#ad48fa;--wave-blue:#4f91ff;--particle:rgba(99,108,222,.64);
    --shadow:0 22px 56px rgba(45,61,96,.13),0 7px 20px rgba(45,61,96,.08);--input-shadow:inset 0 1px 0 rgba(255,255,255,.86);
  }
}
* { box-sizing:border-box; }
html,body { min-height:100%; }
body {
  margin:0;min-height:100vh;overflow-x:hidden;position:relative;padding:58px 24px 42px;
  display:grid;place-items:center;color:var(--text);
  font-family:Outfit,Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  background:
    radial-gradient(circle at 8% 49%,rgba(var(--accent-rgb),.30),transparent 27%),
    radial-gradient(circle at 22% 80%,rgba(var(--accent-rgb),.11),transparent 24%),
    radial-gradient(circle at 92% 47%,rgba(var(--blue-rgb),.27),transparent 28%),
    radial-gradient(circle at 78% 78%,rgba(var(--blue-rgb),.10),transparent 25%),
    linear-gradient(145deg,var(--bg2),var(--bg) 56%,var(--bg2));
}
body::before {
  content:"";position:fixed;z-index:0;inset:0;pointer-events:none;opacity:.25;
  background-image:radial-gradient(circle,rgba(155,126,255,.72) 0 1px,transparent 1.4px),radial-gradient(circle,rgba(61,148,255,.58) 0 1px,transparent 1.4px);
  background-size:72px 72px,108px 108px;background-position:0 0,31px 17px;
  -webkit-mask-image:linear-gradient(90deg,#000 0%,rgba(0,0,0,.20) 38%,rgba(0,0,0,.12) 62%,#000 100%);mask-image:linear-gradient(90deg,#000 0%,rgba(0,0,0,.20) 38%,rgba(0,0,0,.12) 62%,#000 100%);
}
body::after {
  content:"";position:fixed;z-index:0;inset:-10%;pointer-events:none;opacity:.55;
  background:radial-gradient(ellipse at 17% 55%,rgba(var(--accent-rgb),.13),transparent 27%),radial-gradient(ellipse at 84% 54%,rgba(var(--blue-rgb),.12),transparent 29%);
  filter:blur(18px);
}
.version { position:fixed;z-index:4;top:28px;left:34px;color:var(--text3);font-size:12px;font-weight:600;letter-spacing:.015em; }
.auth-backdrop { position:fixed;z-index:1;inset:0;width:100%;height:100%;pointer-events:none;opacity:.97; }
.auth-backdrop .wave { fill:none;stroke-linecap:round;stroke-width:1.05; }
.auth-backdrop .wave-purple { stroke:var(--wave-purple); }
.auth-backdrop .wave-blue { stroke:var(--wave-blue); }
.auth-backdrop .soft { opacity:.17; }.auth-backdrop .mid { opacity:.31; }.auth-backdrop .strong { opacity:.66; }
.auth-backdrop .micro { opacity:.10;stroke-width:.72; }.auth-backdrop .particle { fill:var(--particle); }.auth-backdrop .spark { filter:drop-shadow(0 0 6px currentColor); }
.card {
  position:relative;z-index:2;width:min(760px,calc(100vw - 48px));padding:48px 54px 30px;overflow:hidden;isolation:isolate;
  border:1px solid var(--card-edge);border-radius:12px;
  background:linear-gradient(150deg,var(--card),var(--card2));
  -webkit-backdrop-filter:blur(22px) saturate(132%);backdrop-filter:blur(22px) saturate(132%);
  box-shadow:var(--shadow),inset 0 1px 0 rgba(255,255,255,.055);
}
.card::before {
  content:"";position:absolute;z-index:-1;inset:0;pointer-events:none;
  background:linear-gradient(120deg,rgba(var(--accent-rgb),.075),transparent 28%,transparent 66%,rgba(var(--blue-rgb),.065));
}
.card::after {
  content:"";position:absolute;z-index:-1;left:7%;right:7%;top:-60px;height:115px;pointer-events:none;
  background:radial-gradient(ellipse,rgba(var(--accent-rgb),.13),transparent 60%);filter:blur(18px);
}
.brand-lockup { position:relative;display:flex;flex-direction:column;align-items:center;text-align:center;margin:0 0 22px; }
.brand-pulse { position:absolute;z-index:0;top:9px;left:50%;transform:translateX(-50%);width:min(590px,88%);height:106px;opacity:.74; }
.brand-pulse .grid { stroke:var(--border);stroke-width:.7;opacity:.25; }.brand-pulse .pulse-left { stroke:var(--wave-purple); }.brand-pulse .pulse-right { stroke:var(--wave-blue); }
.brand-pulse .pulse-left,.brand-pulse .pulse-right { fill:none;stroke-width:2;filter:drop-shadow(0 0 6px currentColor); }
.brand-mark { position:relative;z-index:1;width:110px;height:110px;display:block;object-fit:contain;filter:drop-shadow(0 9px 20px rgba(var(--accent-rgb),.24)); }
.brand { position:relative;z-index:1;margin-top:14px;font-size:46px;font-weight:800;letter-spacing:-1.7px;line-height:1;color:var(--text); }
.brand span { color:var(--accent); }.brand-sub { margin-top:13px;color:var(--text2);font-size:19px;font-weight:500; }.sr-only { position:absolute!important;width:1px!important;height:1px!important;padding:0!important;margin:-1px!important;overflow:hidden!important;clip:rect(0,0,0,0)!important;white-space:nowrap!important;border:0!important; }
.error { border:1px solid rgba(255,107,118,.45);background:rgba(255,107,118,.09);color:var(--danger);padding:11px 13px;border-radius:10px;font-size:13px;line-height:1.45;margin:0 0 18px; }
form { margin:0; }
label { display:block;font-size:14px;font-weight:650;color:var(--text2);margin:18px 0 8px; }
.field { position:relative;display:flex;align-items:center; }
.field-icon { position:absolute;z-index:2;left:17px;width:20px;height:20px;color:var(--icon-accent);pointer-events:none;filter:drop-shadow(0 0 8px rgba(var(--accent-rgb),.18)); }
.field-icon svg,.password-reveal svg,.action-arrow svg,.foot-icon svg { display:block;width:100%;height:100%; }
.password-reveal .eye { display:none; }.password-reveal .eye-off { display:block; }
.password-reveal[aria-pressed="true"] .eye { display:block; }.password-reveal[aria-pressed="true"] .eye-off { display:none; }
input {
  width:100%;height:58px;border:1px solid var(--border);background:var(--field);color:var(--text);border-radius:10px;
  padding:0 50px 0 52px;font:inherit;font-size:15px;outline:none;box-shadow:var(--input-shadow);transition:border-color .15s,box-shadow .15s,background .15s;
}
input::placeholder { color:var(--text3);opacity:.86; }
input:focus { border-color:var(--accent);box-shadow:0 0 0 3px rgba(var(--accent-rgb),.13),var(--input-shadow); }
.password-reveal { position:absolute;z-index:3;right:8px;display:grid;place-items:center;width:42px;height:42px;padding:10px;border:0;border-radius:8px;background:transparent;color:var(--icon-accent);cursor:pointer;touch-action:none;filter:drop-shadow(0 0 8px rgba(var(--accent-rgb),.16)); }
.password-reveal:hover,.password-reveal:focus-visible,.password-reveal[aria-pressed="true"] { color:var(--icon-accent-strong);background:rgba(var(--accent-rgb),.09);outline:none; }
.auth-action {
  position:relative;width:100%;min-height:60px;margin-top:24px;border-radius:10px;padding:0 58px;font-weight:700;font-size:16px;cursor:pointer;text-align:center;text-decoration:none;
  display:flex;align-items:center;justify-content:center;gap:14px;font-family:inherit;transition:filter .15s,transform .15s,border-color .15s,background .15s;
}
.auth-action:hover { filter:brightness(1.07); }.auth-action:active { transform:translateY(1px); }
.primary { border:0;background:var(--primary-gradient);color:#fff;box-shadow:0 11px 28px rgba(65,72,255,.23); }.primary:hover { background:var(--primary-gradient-hover); }
.secondary { background:rgba(8,15,31,.42);color:var(--text);border:1px solid var(--border);box-shadow:var(--input-shadow); }.secondary:hover { border-color:var(--border-strong); }
:root[data-theme="light"] .secondary { background:rgba(255,255,255,.56); }
@media (prefers-color-scheme: light) { :root:not([data-theme="dark"]) .secondary { background:rgba(255,255,255,.56); } }
.action-arrow { position:absolute;right:20px;width:21px;height:21px;color:var(--icon-accent-strong);opacity:.92; }
.oidc-mark { position:absolute;left:18px;width:38px;height:38px;object-fit:contain;filter:drop-shadow(0 0 9px rgba(var(--accent-rgb),.18)); }
.oidc.secondary .oidc-separator { position:absolute;left:66px;top:10px;bottom:10px;width:1px;background:var(--border); }
.divider { display:flex;align-items:center;gap:18px;color:var(--text3);font-size:13px;margin:27px 0 0;white-space:nowrap; }.divider:before,.divider:after { content:"";height:1px;background:var(--border);flex:1; }
.muted { color:var(--text3);font-size:13px;line-height:1.55; }
.foot { margin-top:21px;padding:19px 5px 0;border-top:1px solid var(--border);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;color:var(--text3);font-size:12px;line-height:1.65;text-align:center; }
.foot-icon { flex:0 0 21px;width:21px;height:21px;margin:0;color:var(--icon-accent);filter:drop-shadow(0 0 8px rgba(var(--accent-rgb),.16)); }.foot .https { color:var(--accent);font-weight:700; }
.auth-only-message { text-align:center;margin:8px 0 0; }
@media (max-width:700px) {
  body { padding:48px 14px 24px;align-items:start; }.version { top:17px;left:18px;font-size:11px; }
  .card { width:100%;padding:34px 24px 24px;border-radius:12px;-webkit-backdrop-filter:blur(18px) saturate(125%);backdrop-filter:blur(18px) saturate(125%); }.brand-mark { width:86px;height:86px; }.brand { font-size:36px; }.brand-sub { font-size:16px; }
  .brand-pulse { width:98%;top:2px; }.auth-action { min-height:56px; }.oidc-mark { width:34px;height:34px;left:14px; }.oidc.secondary .oidc-separator { left:57px; }
}
@media (max-width:460px) {
  .card { padding-left:17px;padding-right:17px; }.brand { font-size:32px; }.divider { gap:10px;font-size:11px; }.auth-action { font-size:14px;padding-left:52px;padding-right:48px; }
  .foot { font-size:11px; }.auth-backdrop { opacity:.68; }
}
@media (prefers-reduced-motion: reduce) { * { scroll-behavior:auto!important;transition:none!important; } }
"""


_LUCIDE_USER = """<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>"""
_LUCIDE_LOCK = """<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>"""
_LUCIDE_EYE = """<svg class="eye" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0"/><circle cx="12" cy="12" r="3"/></svg>"""
_LUCIDE_EYE_OFF = """<svg class="eye-off" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m2 2 20 20"/><path d="M6.71 6.71C4.69 8.1 3.25 9.85 2.55 11.3a1.6 1.6 0 0 0 0 1.4C4.28 16.27 7.64 19 12 19c1.48 0 2.83-.32 4.03-.87"/><path d="M10.73 5.08A8.7 8.7 0 0 1 12 5c4.36 0 7.72 2.73 9.45 6.3a1.6 1.6 0 0 1 0 1.4 12 12 0 0 1-1.18 1.92"/><path d="M14.12 14.12A3 3 0 0 1 9.88 9.88"/></svg>"""
_LUCIDE_ARROW = """<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14"/><path d="m13 6 6 6-6 6"/></svg>"""
_LUCIDE_SHIELD = """<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 13c0 5-3.5 7.5-8 9-4.5-1.5-8-4-8-9V5l8-3 8 3z"/><path d="m9 12 2 2 4-4"/></svg>"""

_AUTH_BACKDROP_HTML = """<svg class="auth-backdrop" viewBox="0 0 1600 900" preserveAspectRatio="xMidYMid slice" aria-hidden="true">
  <g class="wave-purple spark">
    <path class="wave micro" d="M-90 442 C56 324 128 558 274 422 S477 297 666 444"/>
    <path class="wave soft" d="M-90 468 C62 348 136 580 282 447 S481 319 666 467"/>
    <path class="wave soft" d="M-90 492 C68 372 146 603 291 470 S487 342 666 489"/>
    <path class="wave mid" d="M-90 516 C73 397 154 625 300 493 S492 365 666 512"/>
    <path class="wave mid" d="M-90 540 C79 421 164 647 309 517 S498 389 666 534"/>
    <path class="wave strong" d="M-90 565 C87 445 174 669 320 541 S505 414 666 556"/>
    <path class="wave strong" d="M-90 591 C95 470 187 692 332 566 S514 439 666 580"/>
    <path class="wave mid" d="M-90 617 C106 494 199 714 345 591 S526 465 666 603"/>
    <path class="wave mid" d="M-90 643 C117 519 213 736 359 617 S538 491 666 626"/>
    <path class="wave soft" d="M-90 668 C128 544 227 756 374 642 S551 518 666 648"/>
    <path class="wave soft" d="M-90 694 C141 568 243 775 390 666 S565 545 666 671"/>
    <path class="wave micro" d="M-90 720 C154 591 260 794 406 689 S580 572 666 694"/>
  </g>
  <g class="wave-blue spark">
    <path class="wave micro" d="M934 440 C1066 315 1150 554 1290 422 S1490 306 1694 440"/>
    <path class="wave soft" d="M934 466 C1072 339 1158 578 1298 446 S1496 330 1694 463"/>
    <path class="wave soft" d="M934 491 C1078 365 1166 601 1307 470 S1502 353 1694 486"/>
    <path class="wave mid" d="M934 516 C1084 391 1174 624 1316 494 S1508 377 1694 510"/>
    <path class="wave mid" d="M934 541 C1090 416 1183 647 1326 518 S1515 401 1694 533"/>
    <path class="wave strong" d="M934 566 C1097 441 1193 670 1336 542 S1522 425 1694 556"/>
    <path class="wave strong" d="M934 592 C1104 467 1204 693 1347 567 S1530 450 1694 580"/>
    <path class="wave mid" d="M934 618 C1111 493 1215 715 1359 592 S1539 475 1694 604"/>
    <path class="wave mid" d="M934 644 C1118 519 1227 737 1371 618 S1548 501 1694 628"/>
    <path class="wave soft" d="M934 670 C1125 545 1239 758 1384 643 S1558 528 1694 651"/>
    <path class="wave soft" d="M934 696 C1133 571 1252 779 1398 668 S1568 555 1694 674"/>
    <path class="wave micro" d="M934 722 C1141 597 1265 799 1412 692 S1579 582 1694 697"/>
  </g>
  <g class="particle">
    <circle cx="48" cy="132" r="1.2"/><circle cx="92" cy="207" r="1.7"/><circle cx="137" cy="278" r="2.3"/><circle cx="179" cy="352" r="1.2"/><circle cx="224" cy="391" r="3"/><circle cx="267" cy="246" r="1.5"/><circle cx="315" cy="323" r="1.9"/><circle cx="362" cy="476" r="3.4"/><circle cx="412" cy="171" r="1.3"/><circle cx="458" cy="282" r="2.2"/><circle cx="506" cy="417" r="1.4"/><circle cx="553" cy="355" r="2.7"/><circle cx="601" cy="505" r="1.6"/>
    <circle cx="995" cy="168" r="1.4"/><circle cx="1042" cy="236" r="2.2"/><circle cx="1088" cy="329" r="1.5"/><circle cx="1136" cy="389" r="2.8"/><circle cx="1184" cy="474" r="1.4"/><circle cx="1232" cy="268" r="2.1"/><circle cx="1281" cy="344" r="1.5"/><circle cx="1331" cy="430" r="3.2"/><circle cx="1381" cy="198" r="1.2"/><circle cx="1432" cy="302" r="2.4"/><circle cx="1481" cy="382" r="1.4"/><circle cx="1532" cy="463" r="2.9"/><circle cx="1580" cy="242" r="1.5"/>
    <circle cx="111" cy="553" r="1.3"/><circle cx="188" cy="625" r="2.2"/><circle cx="274" cy="694" r="1.4"/><circle cx="392" cy="604" r="1.7"/><circle cx="1215" cy="611" r="1.5"/><circle cx="1327" cy="681" r="2.2"/><circle cx="1452" cy="597" r="1.6"/><circle cx="1558" cy="690" r="1.3"/>
  </g>
</svg>"""

_BRAND_PULSE_HTML = """<svg class="brand-pulse" viewBox="0 0 600 110" aria-hidden="true">
  <g class="grid"><path d="M0 55h600"/><path d="M60 18v74M120 18v74M180 18v74M240 18v74M300 18v74M360 18v74M420 18v74M480 18v74M540 18v74"/></g>
  <path class="pulse-left" d="M12 55h120l12 9 13-14 14 12 13-14 16 12 13-11 11 5 8-28 9 60 12-45 11 22 19-8h27"/>
  <path class="pulse-right" d="M300 55h34l13-8 9 18 12-44 10 69 13-41 11 21 15-12 18 9 16-15 17 11 16-10 18 2h106"/>
</svg>"""


def _session_lifetime_seconds(cfg) -> int:
    hours = int(getattr(cfg, "auth_session_lifetime_hours", 12) or 12)
    return max(3600, min(168 * 3600, hours * 3600))


def _session_record_current(record, cfg) -> bool:
    if record is None:
        return False
    mechanism = record.principal.mechanism
    if mechanism is AuthMechanism.PASSWORD_SESSION:
        if not password_auth_ready(cfg):
            return False
        current_username = str(getattr(cfg, "auth_username", "") or "").strip()
        if not current_username or record.principal.subject != current_username:
            return False
        current_version = password_credential_version(getattr(cfg, "auth_password_hash", ""))
        return bool(current_version and record.credential_version == current_version)
    if mechanism is AuthMechanism.OIDC_SESSION:
        if not oidc_auth_ready(cfg):
            return False
        current_version = oidc_configuration_version(cfg)
        return bool(current_version and record.credential_version == current_version)
    return False


def _static_asset(name: str) -> Path:
    candidates: list[Path] = []
    configured = os.getenv("STATIC_DIR", "").strip()
    if configured:
        candidates.append(Path(configured) / name)
    candidates.extend(
        (
            Path(__file__).resolve().parents[2] / "frontend" / "static" / name,
            Path("/app/frontend/static") / name,
            Path("/app/static") / name,
        )
    )
    asset = next((candidate for candidate in candidates if candidate.is_file()), None)
    if asset is None:
        raise RuntimeError(f"Frontend asset not found: {name}")
    return asset


def _data_image_html(name: str, mime: str, class_name: str) -> str:
    encoded = base64.b64encode(_static_asset(name).read_bytes()).decode("ascii")
    return (
        f'<img class="{class_name}" alt="" aria-hidden="true" '
        f'src="data:{mime};base64,{encoded}"/>'
    )


def _auth_mark_html() -> str:
    """Build the self-contained login mark from the reviewed large-format asset."""
    encoded = base64.b64encode(_static_asset("logo-128.png").read_bytes()).decode("ascii")
    return (
        '<img class="brand-mark" alt="" aria-hidden="true" '
        f'src="data:image/png;base64,{encoded}"/>'
    )


def _auth_oidc_mark_html() -> str:
    """Embed the reviewed Authentik/OIDC artwork without opening public image access."""
    return _data_image_html("authentik-oidc.svg", "image/svg+xml", "oidc-mark")


def _auth_favicon_html() -> str:
    encoded = base64.b64encode(_static_asset("favicon.svg").read_bytes()).decode("ascii")
    return f'<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,{encoded}">'


_AUTH_MARK_HTML = _auth_mark_html()
_AUTH_OIDC_MARK_HTML = _auth_oidc_mark_html()
_AUTH_FAVICON_HTML = _auth_favicon_html()


def _auth_csp(*, allow_form: bool) -> str:
    directives = [
        "default-src 'none'",
        "img-src data:",
        "style-src 'unsafe-inline'",
        f"script-src 'sha256-{_AUTH_PAGE_SCRIPT_HASH}'",
    ]
    if allow_form:
        directives.append("form-action 'self'")
    directives.extend(("base-uri 'none'", "frame-ancestors 'none'"))
    return "; ".join(directives)


def _login_shell(inner: str, *, title: str = "Sign in · DebridPulse") -> str:
    version = html.escape(read_version())
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>{html.escape(title)}</title>
{_AUTH_FAVICON_HTML}
<style>{_AUTH_PAGE_STYLE}</style>
<script>{_AUTH_PAGE_SCRIPT}</script>
</head>
<body>
<div class="version">v{version}</div>
{_AUTH_BACKDROP_HTML}
{inner}
</body>
</html>"""


def _login_page(
    request: Request,
    *,
    csrf_token: str,
    return_to: str,
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    cfg = get_settings()
    password_enabled = password_auth_enabled(cfg)
    password_ready = password_auth_ready(cfg)
    oidc_enabled = oidc_auth_enabled(cfg)
    oidc_ready = oidc_auth_ready(cfg) if oidc_enabled else False
    provider_name = html.escape(
        str(getattr(cfg, "oidc_provider_name", "") or "OpenID Connect").strip()
        or "OpenID Connect"
    )
    error_html = (
        f'<div class="error" role="alert">{html.escape(error)}</div>' if error else ""
    )

    controls: list[str] = []
    if password_enabled and password_ready:
        controls.append(
            f"""
            <form method="post" action="/login" autocomplete="on">
              <input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}">
              <input type="hidden" name="next" value="{html.escape(return_to, quote=True)}">
              <label for="username">Username</label>
              <div class="field"><span class="field-icon">{_LUCIDE_USER}</span><input id="username" name="username" type="text" maxlength="256" autocomplete="username" placeholder="Enter your username" required></div>
              <label for="password">Password</label>
              <div class="field"><span class="field-icon">{_LUCIDE_LOCK}</span><input id="password" name="password" type="password" maxlength="4096" autocomplete="current-password" placeholder="Enter your password" required><button class="password-reveal" type="button" data-password-reveal aria-label="Hold to show password" aria-pressed="false">{_LUCIDE_EYE}{_LUCIDE_EYE_OFF}</button></div>
              <button class="auth-action primary" type="submit"><span>Sign In</span><span class="action-arrow">{_LUCIDE_ARROW}</span></button>
            </form>
            """
        )
    elif password_enabled:
        controls.append(
            '<div class="error" role="alert">Username &amp; Password authentication is enabled '
            "but is not fully configured. That mechanism is unavailable.</div>"
        )

    if oidc_enabled and oidc_ready:
        if password_ready:
            controls.append('<div class="divider"><span>or continue with single sign-on</span></div>')
        oidc_class = "secondary" if password_ready else "primary"
        separator = '<span class="oidc-separator" aria-hidden="true"></span>' if password_ready else ""
        controls.append(
            f'<a class="auth-action oidc {oidc_class}" href="/auth/oidc/start?next={quote(return_to, safe="")}">'
            f'{_AUTH_OIDC_MARK_HTML}{separator}<span>Continue with {provider_name}</span><span class="action-arrow">{_LUCIDE_ARROW}</span></a>'
        )
    elif oidc_enabled:
        controls.append(
            '<div class="error" role="alert">OpenID Connect is enabled but its local '
            "configuration is incomplete or invalid.</div>"
        )

    if not password_enabled and not oidc_enabled:
        controls.append('<p class="muted auth-only-message">Authentication is not currently required.</p>')

    interactive_controls = "\n".join(controls)
    card = f"""<main class="card">
  <div class="brand-lockup">{_BRAND_PULSE_HTML}{_AUTH_MARK_HTML}<div class="brand">Debrid<span>Pulse</span></div><div class="brand-sub">Sign in to continue</div><span class="sr-only">Secure access</span></div>
  {error_html}
  {interactive_controls}
  <div class="foot"><span class="foot-icon">{_LUCIDE_SHIELD}</span><div>Password-only LAN deployments may operate over HTTP.<br>OpenID Connect requires a canonical <span class="https">HTTPS</span> external URL.</div></div>
</main>"""
    response = HTMLResponse(content=_login_shell(card), status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = _auth_csp(allow_form=True)
    return response


def _state_free_auth_page(
    *,
    message: str,
    status_code: int,
    retry_after: int | None = None,
) -> HTMLResponse:
    """Render an authentication error without allocating browser challenge state."""
    card = f"""<main class="card">
  <div class="brand-lockup">{_BRAND_PULSE_HTML}{_AUTH_MARK_HTML}<div class="brand">Debrid<span>Pulse</span></div><div class="brand-sub">Sign in unavailable</div><span class="sr-only">Secure access</span></div>
  <div class="error" role="alert">{html.escape(message)}</div>
  <a class="auth-action secondary" href="/login"><span>Return to sign in</span><span class="action-arrow">{_LUCIDE_ARROW}</span></a>
</main>"""
    response = HTMLResponse(
        content=_login_shell(card, title="Sign in unavailable · DebridPulse"),
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = _auth_csp(allow_form=False)
    if retry_after is not None:
        response.headers["Retry-After"] = str(max(1, int(retry_after)))
    return response


def _issue_login_page(
    request: Request,
    *,
    return_to: str,
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    if not login_challenge_rate_limiter.allow(peer_key(request)):
        return _state_free_auth_page(
            message="Too many sign-in challenges have been requested. Try again shortly.",
            status_code=429,
            retry_after=60,
        )
    browser_nonce, form_token = login_csrf_store.issue()
    response = _login_page(
        request,
        csrf_token=form_token,
        return_to=safe_return_path(return_to),
        error=error,
        status_code=status_code,
    )
    set_login_csrf_cookie(response, request, browser_nonce)
    return response


def _set_oidc_correlation_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        key=OIDC_CORRELATION_COOKIE,
        value=str(value),
        max_age=OIDC_TRANSACTION_TTL_SECONDS,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _clear_oidc_correlation_cookie(response: Response) -> None:
    response.delete_cookie(
        key=OIDC_CORRELATION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


@router.get("/api/auth/status")
async def public_auth_status():
    """Minimal public bootstrap state needed to render the login experience."""
    cfg = get_settings()
    password_enabled = password_auth_enabled(cfg)
    oidc_enabled = oidc_auth_enabled(cfg)
    return {
        "authentication_required": interactive_auth_enabled(cfg),
        "password_enabled": password_enabled,
        "password_ready": password_auth_ready(cfg) if password_enabled else False,
        "oidc_enabled": oidc_enabled,
        "oidc_ready": oidc_auth_ready(cfg) if oidc_enabled else False,
        "oidc_provider_name": (
            str(getattr(cfg, "oidc_provider_name", "") or "OpenID Connect").strip()
            or "OpenID Connect"
        ),
    }


@router.get("/app.js", include_in_schema=False)
async def application_javascript_bundle():
    """Serve the protected browser bootstrap before the existing app script."""
    auth_js = _static_asset("auth.js").read_text(encoding="utf-8")
    app_js = _static_asset("app.js").read_text(encoding="utf-8")
    response = Response(
        content=f"{auth_js}\n;\n{app_js}",
        media_type="application/javascript",
    )
    response.headers["Cache-Control"] = "no-cache"
    return response

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    cfg = get_settings()
    return_to = safe_return_path(next)
    if not interactive_auth_enabled(cfg):
        return RedirectResponse(url=return_to, status_code=303)

    existing_token = session_cookie_token(request)
    existing = session_store.resolve(existing_token) if existing_token else None
    if _session_record_current(existing, cfg):
        return RedirectResponse(url=return_to, status_code=303)
    if existing_token:
        session_store.revoke(existing_token)

    response = _issue_login_page(request, return_to=return_to)
    if existing_token:
        clear_session_cookie(response, request)
    return response


@router.post("/login")
async def password_login(request: Request):
    cfg = get_settings()
    if not password_auth_enabled(cfg):
        return _issue_login_page(
            request,
            return_to="/",
            error="Username & Password authentication is disabled.",
            status_code=403,
        )
    if not password_auth_ready(cfg):
        return _issue_login_page(
            request,
            return_to="/",
            error="Username & Password authentication is unavailable because its configuration is incomplete.",
            status_code=503,
        )

    form = await request.form()
    username = str(form.get("username") or "")
    password = str(form.get("password") or "")
    csrf_token = str(form.get("csrf_token") or "")
    return_to = safe_return_path(str(form.get("next") or "/"))

    if len(username) > 256 or len(password) > 4096 or len(csrf_token) > 256:
        return _issue_login_page(
            request,
            return_to=return_to,
            error="Invalid sign-in request.",
            status_code=400,
        )

    browser_nonce = str(request.cookies.get(login_csrf_cookie_name(request), "") or "")
    if not login_csrf_store.consume(browser_nonce, csrf_token):
        return _issue_login_page(
            request,
            return_to=return_to,
            error="The sign-in form expired. Try again.",
            status_code=403,
        )

    try:
        verified = await verify_local_credentials(
            request,
            username,
            password,
            settings=cfg,
        )
    except PasswordAuthenticationBusy:
        response = _issue_login_page(
            request,
            return_to=return_to,
            error="Too many sign-in attempts are already being processed. Try again shortly.",
            status_code=429,
        )
        response.headers["Retry-After"] = "2"
        return response
    if not verified:
        return _issue_login_page(
            request,
            return_to=return_to,
            error="Invalid username or password.",
            status_code=401,
        )

    async with authentication_configuration_lock:
        current = get_settings()
        if not password_authentication_snapshot_current(cfg, current):
            return _issue_login_page(
                request,
                return_to=return_to,
                error="Authentication configuration changed while sign-in was in progress. Try again.",
                status_code=409,
            )

        old_token = session_cookie_token(request)
        if old_token:
            session_store.revoke(old_token)

        configured_username = str(getattr(current, "auth_username", "") or "").strip()
        lifetime = _session_lifetime_seconds(current)
        version = password_credential_version(getattr(current, "auth_password_hash", ""))
        token, _record = session_store.create(
            Principal.password_session(configured_username, credential_version=version),
            lifetime_seconds=lifetime,
            credential_version=version,
        )
    response = RedirectResponse(url=return_to, status_code=303)
    set_session_cookie(response, request, token, max_age=lifetime)
    clear_login_csrf_cookie(response, request)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/auth/oidc/start")
async def oidc_start(request: Request, next: str = "/"):
    cfg = get_settings()
    return_to = safe_return_path(next)
    if not oidc_auth_enabled(cfg):
        return _issue_login_page(
            request,
            return_to=return_to,
            error="OpenID Connect authentication is disabled.",
            status_code=404,
        )
    if not oidc_start_rate_limiter.allow(peer_key(request)):
        return _state_free_auth_page(
            message="Too many OpenID Connect sign-in attempts have been started. Try again shortly.",
            status_code=429,
            retry_after=60,
        )
    try:
        authorization_url, correlation = await begin_oidc_login(cfg, return_to=return_to)
    except OidcError:
        return _issue_login_page(
            request,
            return_to=return_to,
            error="OpenID Connect is currently unavailable or misconfigured.",
            status_code=503,
        )
    response = RedirectResponse(url=authorization_url, status_code=303)
    _set_oidc_correlation_cookie(response, correlation)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/auth/oidc/callback")
async def oidc_callback(
    request: Request,
    state: str = "",
    code: str = "",
    error: str = "",
):
    correlation = str(request.cookies.get(OIDC_CORRELATION_COOKIE, "") or "")
    if error:
        oidc_transaction_store.consume(state, correlation)
        response = _issue_login_page(
            request,
            return_to="/",
            error="OpenID Connect sign-in was not completed.",
            status_code=401,
        )
        _clear_oidc_correlation_cookie(response)
        return response

    try:
        principal, return_to = await complete_oidc_login(
            state=state,
            code=code,
            correlation=correlation,
        )
    except OidcError:
        response = _issue_login_page(
            request,
            return_to="/",
            error="OpenID Connect sign-in could not be validated or authorized.",
            status_code=401,
        )
        _clear_oidc_correlation_cookie(response)
        return response

    async with authentication_configuration_lock:
        cfg = get_settings()
        current_version = oidc_configuration_version(cfg)
        proof_version = str(principal.credential_version or "")
        if not proof_version or not current_version or not secrets.compare_digest(
            proof_version,
            current_version,
        ):
            response = _issue_login_page(
                request,
                return_to="/",
                error="Authentication configuration changed while sign-in was in progress. Start a new sign-in.",
                status_code=409,
            )
            _clear_oidc_correlation_cookie(response)
            return response

        old_token = session_cookie_token(request)
        if old_token:
            session_store.revoke(old_token)
        lifetime = _session_lifetime_seconds(cfg)
        token, _record = session_store.create(
            principal,
            lifetime_seconds=lifetime,
            credential_version=proof_version,
        )

    response = RedirectResponse(url=safe_return_path(return_to), status_code=303)
    set_session_cookie(
        response,
        request,
        token,
        max_age=lifetime,
        force_secure=True,
    )
    clear_login_csrf_cookie(response, request)
    _clear_oidc_correlation_cookie(response)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/api/auth/session")
async def auth_session_status(request: Request, response: Response = None):
    principal = getattr(request.state, "principal", Principal.anonymous())
    session_token = str(getattr(request.state, "auth_session_token", "") or "")
    record = session_store.resolve(session_token) if session_token else None
    if response is not None:
        response.headers["Cache-Control"] = "no-store"
    return {
        "authenticated": bool(principal.authenticated),
        "mechanism": principal.mechanism.value if principal.mechanism else None,
        "subject": principal.subject,
        "display_name": principal.display_name,
        "csrf_token": session_store.csrf_token(session_token) if record is not None else "",
        "session_expires_in_seconds": (
            max(0, int(record.expires_at - time.monotonic())) if record is not None else None
        ),
    }


@router.post("/api/auth/logout")
async def logout(request: Request):
    principal = getattr(request.state, "principal", Principal.anonymous())
    if principal.mechanism not in {AuthMechanism.PASSWORD_SESSION, AuthMechanism.OIDC_SESSION}:
        return JSONResponse(
            content={"detail": "No browser application session"},
            status_code=400,
        )

    session_token = str(getattr(request.state, "auth_session_token", "") or "")
    if session_token:
        session_store.revoke(session_token)
    response = JSONResponse(content={"ok": True})
    clear_session_cookie(response, request)
    response.headers["Cache-Control"] = "no-store"
    return response