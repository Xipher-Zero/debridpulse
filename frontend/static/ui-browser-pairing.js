/* DownloadPulse browser-pairing approval owner (WS-S1).
 *
 * The ONE owner of the "Connect a browser" dialog. A browser extension that
 * asks to connect opens this server at `/?connect-browser=<code>`; signing in
 * first returns to that same address. At boot this owner reads the code once,
 * removes it from the address bar, and asks the signed-in person -- in the
 * shared dialog shell (ui-settings-modal.js) -- whether to connect that
 * browser to THEIR OWN account:
 *
 *   - it shows what the server says about the request: the browser's name,
 *     when it asked, and the code the extension also shows;
 *   - Connect sends the editable name; Deny refuses; closing answers nothing
 *     (the request simply expires);
 *   - a person who may not connect browsers (ReadOnly) is told so, and asked
 *     nothing.
 *
 * The server decides everything (api/browser_routes.py) and issues the token
 * to the extension, never to this page. Nothing here persists or reads any
 * other state.
 */
(function () {
  'use strict';

  const PARAM = 'connect-browser';
  const said = key => ({key, values: null, toString: () => DPI18n.t(key)});
  const escape = value => String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));

  function takeCode() {
    const url = new URL(window.location.href);
    const code = String(url.searchParams.get(PARAM) || '').trim();
    if (!url.searchParams.has(PARAM)) return '';
    url.searchParams.delete(PARAM);
    window.history.replaceState(window.history.state, '', url.pathname + url.search + url.hash);
    return code.slice(0, 32);
  }

  async function answer(method, path, body) {
    const response = await window.downloadPulseAuth.fetch(path, {
      method, headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body)});
    let data = null;
    try { data = await response.json(); } catch (_) { data = null; }
    return {ok: response.ok, data};
  }

  function refusalText(data) {
    const code = data && typeof data.code === 'string' ? data.code : '';
    return code && DPI18n.has?.(`error.${code}`) ? DPI18n.t(`error.${code}`, data.params || {})
      : DPI18n.t('browser.pairing.failed');
  }

  function tell(key) {
    window.DPSettingsModal.open({
      title: said('browser.pairing.title'),
      dismiss: true,
      className: 'dp-browser-pairing-dialog',
      mount(body) {
        body.innerHTML = `<p class="dp-browser-pairing-message" data-i18n="${key}"></p>`;
        DPI18n.render(body);
      },
    });
  }

  async function ask(code) {
    const modal = window.DPSettingsModal;
    if (!window.downloadPulseAuth.can('browsers.own')) { tell('browser.pairing.not_permitted'); return; }
    const shown = await answer('GET', `/api/account/browser-pairings/${encodeURIComponent(code)}`);
    if (!shown.ok || !shown.data) {
      modal.open({title: said('browser.pairing.title'), dismiss: true, className: 'dp-browser-pairing-dialog',
        mount(body) {
          body.innerHTML = '<p class="dp-browser-pairing-message"></p>';
          body.firstElementChild.textContent = refusalText(shown.data);
        }});
      return;
    }
    const request = shown.data;
    const requested = window.downloadsDateMarkup ? window.downloadsDateMarkup(request.requested_at)
      : escape(request.requested_at);
    let nameField = null;
    let handle = null;
    modal.open({
      title: said('browser.pairing.title'),
      acceptLabel: said('browser.pairing.approve'),
      cancelLabel: said('common.cancel'),
      className: 'dp-browser-pairing-dialog',
      actions: [{id: 'deny', label: said('browser.pairing.deny'), tone: 'danger', placement: 'leading'}],
      async onAction(id) {
        if (id !== 'deny') return;
        handle.setBusy(true);
        const denied = await answer('POST', `/api/account/browser-pairings/${encodeURIComponent(request.user_code)}/deny`);
        handle.setBusy(false);
        window.toast(denied.ok ? DPI18n.t('browser.pairing.denied') : refusalText(denied.data), denied.ok ? 'success' : 'error');
        if (denied.ok) handle.close();
      },
      mount(body, dialogHandle) {
        handle = dialogHandle;
        body.innerHTML = `
          <p class="dp-browser-pairing-intro" data-i18n="browser.pairing.intro"></p>
          <dl class="dp-browser-pairing-facts">
            <div><dt data-i18n="browser.pairing.field.browser"></dt><dd data-pairing-browser></dd></div>
            <div><dt data-i18n="browser.pairing.field.requested"></dt><dd>${requested}</dd></div>
            <div><dt data-i18n="browser.pairing.field.code"></dt><dd class="dp-browser-pairing-code" data-pairing-code></dd></div>
          </dl>
          <p class="dp-browser-pairing-hint" data-i18n="browser.pairing.code_hint"></p>
          <label class="dp-modal-field"><span class="form-label" data-i18n="browser.pairing.field.name"></span>
            <input class="input" data-pairing-name type="text" maxlength="128" autocomplete="off"></label>
          <p class="dp-browser-pairing-warning" role="note" data-i18n="browser.pairing.warning"></p>`;
        DPI18n.render(body);
        body.querySelector('[data-pairing-browser]').textContent = String(request.client_name || '');
        body.querySelector('[data-pairing-code]').textContent = String(request.user_code || '');
        nameField = body.querySelector('[data-pairing-name]');
        nameField.value = String(request.client_name || '');
        return nameField;
      },
      async onAccept() {
        const label = nameField.value.trim();
        if (!label) { window.toast(DPI18n.t('error.account.browser_token_label_required'), 'error'); return false; }
        const approved = await answer('POST',
          `/api/account/browser-pairings/${encodeURIComponent(request.user_code)}/approve`, {label});
        if (!approved.ok) { window.toast(refusalText(approved.data), 'error'); return false; }
        window.toast(DPI18n.t('browser.pairing.approved', {name: approved.data.browser.label}), 'success');
        return true;
      },
    });
  }

  function start() {
    const code = takeCode();
    if (!code || !window.DPSettingsModal || !window.downloadPulseAuth) return;
    void ask(code);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once: true});
  else start();
})();
