from pathlib import Path
import py_compile


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one replacement target, found {count}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


# Backend auth payload: expose durable exact-config OIDC proof while preserving
# proof across a simple enable-toggle cycle.
path = "backend/api/auth_config_routes.py"
replace_once(
    path,
    "from auth.oidc_version import (\n",
    "from auth.oidc_verification import oidc_verification_store\nfrom auth.oidc_version import (\n",
)
replace_once(
    path,
    '''def _local_oidc_state(cfg) -> tuple[bool, str]:
    candidate = cfg.model_copy(update={"auth_oidc_enabled": True}, deep=True)
    try:
        oidc_configuration(candidate)
        return True, oidc_callback_url(candidate)
    except OidcError:
        return False, ""


async def _oidc_runtime_available''',
    '''def _local_oidc_state(cfg) -> tuple[bool, str]:
    candidate = cfg.model_copy(update={"auth_oidc_enabled": True}, deep=True)
    try:
        oidc_configuration(candidate)
        return True, oidc_callback_url(candidate)
    except OidcError:
        return False, ""


def _oidc_configuration_version_for_status(cfg) -> str:
    """Fingerprint configured OIDC even while its enable toggle is off."""
    candidate = cfg.model_copy(update={"auth_oidc_enabled": True}, deep=True)
    return oidc_configuration_version(candidate)


async def _oidc_runtime_available''',
)
replace_once(
    path,
    '''    oidc_configured, callback_url = _local_oidc_state(cfg)
    principal = getattr(request.state, "principal", Principal.anonymous())''',
    '''    oidc_configured, callback_url = _local_oidc_state(cfg)
    oidc_version = _oidc_configuration_version_for_status(cfg) if oidc_configured else ""
    oidc_verification = oidc_verification_store.status(oidc_version)
    oidc_available = await _oidc_runtime_available(cfg, oidc_configured)
    principal = getattr(request.state, "principal", Principal.anonymous())''',
)
replace_once(
    path,
    '''        "oidc_ready": oidc_auth_ready(cfg) if oidc_auth_enabled(cfg) else False,
        "oidc_available": await _oidc_runtime_available(cfg, oidc_configured),''',
    '''        "oidc_ready": oidc_auth_ready(cfg) if oidc_auth_enabled(cfg) else False,
        "oidc_available": oidc_available,
        "oidc_verified": oidc_verification.verified,
        "oidc_verified_at": oidc_verification.verified_at,''',
)

# Clean Settings runtime remains the sole owner of the auth snapshot; project
# the four state KPIs directly from that authoritative payload.
path = "frontend/static/ui-settings-page.js"
p = Path(path)
text = p.read_text(encoding="utf-8")
start = text.index("  function authStatusCard(a) {")
end = text.index("\n  function authenticationPanel(a) {", start)
new_function = r'''  function authStatusCard(a) {
    const callback = a.oidc_callback_url || 'Configure External Base URL to derive callback';
    const modeRaw = String(a.mode || 'Unknown');
    const modeValue = modeRaw === 'OIDC'
      ? 'OpenID Connect'
      : modeRaw === 'No authentication'
        ? 'No Authentication'
        : modeRaw;

    const passwordOperational = !!a.password_enabled && !!a.password_ready;
    const oidcOperational = !!a.oidc_enabled && !!a.oidc_ready && a.oidc_available !== false;

    let modeTone = 'neutral';
    if (a.authentication_required) {
      modeTone = passwordOperational || oidcOperational ? 'green' : 'red';
    }

    let passwordValue = 'Not Configured';
    let passwordTone = 'neutral';
    if (a.password_configured) {
      if (!a.password_enabled) {
        passwordValue = 'Configured';
        passwordTone = 'yellow';
      } else if (a.password_ready) {
        passwordValue = 'Configured & Enabled';
        passwordTone = 'green';
      } else {
        passwordValue = 'Configuration Error';
        passwordTone = 'red';
      }
    } else if (a.password_enabled) {
      passwordValue = 'Configuration Error';
      passwordTone = 'red';
    }

    let oidcValue = 'Not Configured';
    let oidcTone = 'neutral';
    if (a.oidc_configured) {
      if (!a.oidc_enabled) {
        oidcValue = 'Configured';
        oidcTone = 'yellow';
      } else if (a.oidc_available === false) {
        oidcValue = a.oidc_verified ? 'Verified · Runtime Unavailable' : 'Runtime Unavailable';
        oidcTone = 'red';
      } else if (a.oidc_verified) {
        oidcValue = 'Enabled & Verified';
        oidcTone = 'green';
      } else if (a.oidc_ready) {
        oidcValue = 'Configured & Enabled';
        oidcTone = 'yellow';
      } else {
        oidcValue = 'Configuration Error';
        oidcTone = 'red';
      }
    } else if (a.oidc_enabled) {
      oidcValue = 'Configuration Error';
      oidcTone = 'red';
    }

    let tokenValue = 'Not Configured';
    let tokenTone = 'neutral';
    if (a.api_token_configured) {
      if (a.api_token_enabled) {
        tokenValue = 'Configured & Enabled';
        tokenTone = 'green';
      } else {
        tokenValue = 'Configured';
        tokenTone = 'yellow';
      }
    } else if (a.api_token_enabled) {
      tokenValue = 'Configuration Error';
      tokenTone = 'red';
    }

    const items = [
      ['Authentication Mode', modeValue, modeTone],
      ['Username & Password', passwordValue, passwordTone],
      ['OIDC State', oidcValue, oidcTone],
      ['API Token', tokenValue, tokenTone],
    ];
    return card('Authentication Status', `
      ${!a.authentication_required ? `
        <div class="dp-settings-caution dp-settings-auth-open-notice">
          <span><b>No interactive authentication enabled</b> — supported standalone/LAN mode; application and API are intentionally open.</span>
        </div>` : ''}
      <div class="dp-settings-status-grid dp-settings-auth-kpi-grid">
        ${items.map(([label, value, tone]) => `
          <div class="dash-hero-stat dp-settings-auth-kpi" data-c="${html(tone)}">
            <div class="dhs-body">
              <div class="dhs-label">${html(label)}</div>
              <div class="dhs-val">${html(value)}</div>
            </div>
          </div>`).join('')}
      </div>
      <div class="dp-settings-field">
        <label class="form-label">OIDC Callback URL</label>
        <input class="input" value="${html(callback)}" readonly>
      </div>
    `);
  }
'''
p.write_text(text[:start] + new_function + text[end:], encoding="utf-8")

# OIDC copy cleanup and full-card visual centering for Access Control.
path = "frontend/static/ui-settings-authentication-oidc.js"
replace_once(
    path,
    "    setFieldCopy(secret, 'Client Secret', 'Leave blank to keep the stored secret. Enter a new value to replace it.');\n",
    "    setFieldCopy(secret, 'Client Secret', 'Leave blank to keep the stored secret. Enter a new value to replace it.');\n"
    "    const secretInput = secret.querySelector('#dp-auth-oidc-secret');\n"
    "    if (secretInput && secret.querySelector('#dp-auth-clear-oidc-secret')) {\n"
    "      secretInput.placeholder = 'Stored Client Secret Configured. Blank keeps it.';\n"
    "    }\n",
)

path = "frontend/static/ui-settings-authentication-oidc.css"
replace_once(
    path,
    '''body.dp-v11-structural #view-settings .dp-settings-oidc-section-heading {
  min-width: 0;
  display: grid;
  grid-template-columns: minmax(160px, .6fr) minmax(420px, 1.4fr) minmax(390px, 1fr);
  align-items: center;
  gap: 18px;
}''',
    '''body.dp-v11-structural #view-settings .dp-settings-oidc-section-heading {
  min-width: 0;
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  align-items: center;
  gap: 18px;
}''',
)
replace_once(
    path,
    '''body.dp-v11-structural #view-settings .dp-settings-oidc-section-copy {
  justify-self: center;
  color: var(--dp-text-muted);
  font-size: 11px;
  line-height: 1.4;
  text-align: center;
}''',
    '''body.dp-v11-structural #view-settings .dp-settings-oidc-section-copy {
  position: absolute;
  left: 50%;
  top: 50%;
  width: min(620px, 44%);
  transform: translate(-50%, -50%);
  color: var(--dp-text-muted);
  font-size: 11px;
  line-height: 1.4;
  text-align: center;
}''',
)
replace_once(
    path,
    '''  body.dp-v11-structural #view-settings .dp-settings-oidc-section-heading {
    grid-template-columns: auto minmax(300px, 1fr) auto;
  }''',
    '''  body.dp-v11-structural #view-settings .dp-settings-oidc-section-copy {
    width: min(500px, 40%);
  }''',
)
replace_once(
    path,
    '''  body.dp-v11-structural #view-settings .dp-settings-oidc-clear-secret-action,
  body.dp-v11-structural #view-settings .dp-settings-oidc-section-title,
  body.dp-v11-structural #view-settings .dp-settings-oidc-section-copy,
  body.dp-v11-structural #view-settings .dp-settings-oidc-section-heading > .dp-settings-oidc-allow-all {
    justify-self: start;
  }''',
    '''  body.dp-v11-structural #view-settings .dp-settings-oidc-clear-secret-action,
  body.dp-v11-structural #view-settings .dp-settings-oidc-section-title,
  body.dp-v11-structural #view-settings .dp-settings-oidc-section-heading > .dp-settings-oidc-allow-all {
    justify-self: start;
  }

  body.dp-v11-structural #view-settings .dp-settings-oidc-section-copy {
    position: static;
    width: auto;
    transform: none;
    justify-self: start;
  }''',
)

# Settings-scoped adapter for the existing Dashboard KPI classes.
path = "frontend/static/ui-settings-authentication.css"
replace_once(
    path,
    "/* Authentication Status --------------------------------------------------- */\n",
    r'''/* Authentication Status --------------------------------------------------- */
body.dp-v11-structural #view-settings .dp-settings-auth-kpi-grid {
  min-width: 0;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

/* Reuse the Dashboard KPI component classes/material language while adapting
   only the state-card geometry: no icon, no sparkline, centered two-line copy. */
body.dp-v11-structural #view-settings .dp-settings-auth-kpi.dash-hero-stat {
  --c: var(--dp-text-muted);
  min-width: 0;
  min-height: 76px;
  position: relative;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 13px 16px 12px;
  border: 1px solid rgba(105, 119, 181, .23);
  border-radius: var(--dp-radius-lg);
  background:
    linear-gradient(180deg,
      color-mix(in srgb, var(--c) 12%, transparent) 0%,
      color-mix(in srgb, var(--c) 5%, transparent) 24%,
      transparent 48%),
    linear-gradient(145deg, rgba(17, 22, 51, .96), rgba(9, 14, 35, .98));
  box-shadow:
    inset 0 1px rgba(255,255,255,.025),
    -7px 9px 24px -14px rgba(0, 0, 14, .42),
    0 7px 20px rgba(0,0,12,.055);
  transform: none;
  transition: none;
}

body.light.dp-v11-structural #view-settings .dp-settings-auth-kpi.dash-hero-stat {
  border-color: #dce1ef;
  background:
    linear-gradient(180deg,
      color-mix(in srgb, var(--c) 9%, white) 0%,
      color-mix(in srgb, var(--c) 3%, white) 27%,
      #ffffff 58%,
      #f8f9fd 100%);
  box-shadow:
    inset 0 1px rgba(255,255,255,.70),
    -8px 10px 25px -14px rgba(64, 73, 110, .22),
    0 6px 18px rgba(70, 79, 113, .055);
}

body.dp-v11-structural #view-settings .dp-settings-auth-kpi.dash-hero-stat::before {
  display: block;
  content: '';
  position: absolute;
  z-index: 4;
  left: -1px;
  top: -1px;
  width: calc(100% - 19px);
  height: 10px;
  border-top: 3px solid var(--c);
  border-left: 1.5px solid color-mix(in srgb, var(--c) 72%, transparent);
  border-top-left-radius: 12px;
  background: transparent;
  box-shadow: 0 -1px 8px color-mix(in srgb, var(--c) 24%, transparent);
  -webkit-mask-image: linear-gradient(90deg,
    #000 0%, #000 87%, rgba(0,0,0,.80) 93%, transparent 100%);
  mask-image: linear-gradient(90deg,
    #000 0%, #000 87%, rgba(0,0,0,.80) 93%, transparent 100%);
  pointer-events: none;
}

body.dp-v11-structural #view-settings .dp-settings-auth-kpi[data-c="green"] {
  --c: var(--dp-semantic-success);
}
body.dp-v11-structural #view-settings .dp-settings-auth-kpi[data-c="yellow"] {
  --c: var(--dp-semantic-processing);
}
body.dp-v11-structural #view-settings .dp-settings-auth-kpi[data-c="red"] {
  --c: var(--dp-semantic-error);
}
body.dp-v11-structural #view-settings .dp-settings-auth-kpi[data-c="neutral"] {
  --c: var(--dp-text-muted);
}

body.dp-v11-structural #view-settings .dp-settings-auth-kpi .dhs-body {
  min-width: 0;
  width: 100%;
  display: grid;
  justify-items: center;
  gap: 5px;
  text-align: center;
}

body.dp-v11-structural #view-settings .dp-settings-auth-kpi .dhs-label {
  min-width: 0;
  color: var(--dp-text-primary);
  font-size: 11px;
  font-weight: 750;
  line-height: 1.25;
  letter-spacing: 0;
  text-transform: none;
  text-align: center;
}

body.dp-v11-structural #view-settings .dp-settings-auth-kpi .dhs-val {
  min-width: 0;
  color: var(--dp-text-muted);
  font-size: 14px;
  font-weight: 650;
  line-height: 1.25;
  text-align: center;
  overflow-wrap: anywhere;
}

body.dp-v11-structural #view-settings .dp-settings-auth-open-notice {
  min-height: 0;
  display: block;
  padding: 7px 12px;
}

body.dp-v11-structural #view-settings .dp-settings-auth-open-notice > span {
  display: block;
  line-height: 1.4;
}

''',
)
replace_once(
    path,
    '''  body.dp-v11-structural #view-settings .dp-settings-auth-status-card .dp-settings-auth-session-row {
    grid-template-columns: minmax(190px, 1fr) minmax(230px, 1.1fr) minmax(230px, .9fr) auto;
    gap: 18px;
  }''',
    '''  body.dp-v11-structural #view-settings .dp-settings-auth-kpi-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  body.dp-v11-structural #view-settings .dp-settings-auth-status-card .dp-settings-auth-session-row {
    grid-template-columns: minmax(190px, 1fr) minmax(230px, 1.1fr) minmax(230px, .9fr) auto;
    gap: 18px;
  }''',
)
replace_once(
    path,
    '''  body.dp-v11-structural #view-settings .dp-settings-auth-status-card .dp-settings-auth-session-row {
    grid-template-columns: minmax(0, 1fr);
    gap: 16px;
  }''',
    '''  body.dp-v11-structural #view-settings .dp-settings-auth-kpi-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  body.dp-v11-structural #view-settings .dp-settings-auth-status-card .dp-settings-auth-session-row {
    grid-template-columns: minmax(0, 1fr);
    gap: 16px;
  }''',
)

# Static presentation contract for the four-card status instrument.
Path("backend/tests/test_settings_authentication_kpi_ui.py").write_text(
    r'''from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "frontend" / "static"
SETTINGS = STATIC / "ui-settings-page.js"
STYLE = STATIC / "ui-settings-authentication.css"
OIDC_JS = STATIC / "ui-settings-authentication-oidc.js"
OIDC_CSS = STATIC / "ui-settings-authentication-oidc.css"


def source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_authentication_status_uses_four_dashboard_derived_state_kpis():
    js = source(SETTINGS)
    css = source(STYLE)
    block = js[js.index("function authStatusCard"):js.index("function authenticationPanel")]

    assert "['Authentication Mode', modeValue, modeTone]" in block
    assert "['Username & Password', passwordValue, passwordTone]" in block
    assert "['OIDC State', oidcValue, oidcTone]" in block
    assert "['API Token', tokenValue, tokenTone]" in block
    assert 'class="dash-hero-stat dp-settings-auth-kpi"' in block
    assert "dhs-body" in block and "dhs-label" in block and "dhs-val" in block
    assert "dhs-icon" not in block
    assert "dp-card-spark" not in block
    assert "grid-template-columns: repeat(4, minmax(0, 1fr));" in css
    assert ".dp-settings-auth-kpi.dash-hero-stat::before" in css


def test_authentication_kpi_state_ladders_are_semantic_and_truthful():
    js = source(SETTINGS)
    css = source(STYLE)

    assert "passwordValue = 'Configured & Enabled';" in js
    assert "oidcValue = 'Enabled & Verified';" in js
    assert "oidcValue = a.oidc_verified ? 'Verified · Runtime Unavailable' : 'Runtime Unavailable';" in js
    assert "tokenValue = 'Configured & Enabled';" in js
    assert "Configuration Error" in js
    assert 'data-c="${html(tone)}"' in js
    assert '[data-c="green"]' in css and "var(--dp-semantic-success)" in css
    assert '[data-c="yellow"]' in css and "var(--dp-semantic-processing)" in css
    assert '[data-c="red"]' in css and "var(--dp-semantic-error)" in css
    assert '[data-c="neutral"]' in css


def test_open_auth_notice_is_compact_single_line_copy():
    js = source(SETTINGS)
    css = source(STYLE)
    assert "No interactive authentication enabled</b> — supported standalone/LAN mode; application and API are intentionally open." in js
    assert ".dp-settings-auth-open-notice" in css
    assert "padding: 7px 12px;" in css


def test_oidc_minor_copy_and_access_centering_are_locked():
    js = source(OIDC_JS)
    css = source(OIDC_CSS)
    assert "Stored Client Secret Configured. Blank keeps it." in js
    assert ".dp-settings-oidc-section-copy" in css
    assert "position: absolute;" in css
    assert "left: 50%;" in css
    assert "transform: translate(-50%, -50%);" in css
    assert "position: static;" in css
''',
    encoding="utf-8",
)

py_compile.compile("backend/api/auth_config_routes.py", doraise=True)
py_compile.compile("backend/auth/oidc_verification.py", doraise=True)
py_compile.compile("backend/auth/pending_oidc.py", doraise=True)
py_compile.compile("backend/tests/test_auth_oidc_verification_state.py", doraise=True)
py_compile.compile("backend/tests/test_settings_authentication_kpi_ui.py", doraise=True)
