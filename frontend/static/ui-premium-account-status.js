/* Premium account detail: the ONE owner of the Provider Status premium row.
 *
 * The neutral status owner (ui-provider-status.js) observes every provider and
 * publishes what it saw; it never interprets an account. This owner reads that
 * publication and composes every healthy premium account -- in the neutral
 * owner's provider order -- under the row's single crown: one account as the
 * full "<Provider> <Tier> until <date>" / "(N days remaining)" block, several
 * as one compact "<Provider> <Tier> N days remaining" line each. No eligible
 * account hides the row; a provider that is not healthy contributes nothing,
 * whatever it reported before.
 *
 * Interpreting a provider's account facts is the only provider-specific thing
 * here, and it stops at the expiry instant each status surface publishes. An
 * access-token lifetime is never an account fact. This is also the one
 * interpreter and wording a provider's Settings card uses for the same expiry
 * (window.DPPremiumAccount), so the two surfaces can never disagree.
 */
(function () {
  'use strict';

  // Each provider's own status payload -> its premium account ({until, tier}),
  // or null when it has no displayable premium time.
  const PREMIUM_ACCOUNT = Object.freeze({
    alldebrid: status => {
      const until = Number(status?.premiumUntil || status?.premium_until || 0);
      return status?.isPremium && until > 0 ? {until: new Date(until * 1000), tier: 'Premium'} : null;
    },
    realdebrid: status => {
      const until = status?.premium ? new Date(String(status?.expiration || '')) : null;
      const type = String(status?.account_type || 'premium');
      return until && !Number.isNaN(until.getTime())
        ? {until, tier: type.charAt(0).toUpperCase() + type.slice(1)} : null;
    },
    // TorBox names the plan itself (Essential, Standard, Pro); its expiry is
    // the plan's own end, never the API token's.
    torbox: status => {
      const until = status?.premium ? new Date(String(status?.premium_expires_at || '')) : null;
      return until && !Number.isNaN(until.getTime())
        ? {until, tier: String(status?.plan_name || 'Premium')} : null;
    },
  });

  // One provider's premium account from its own status payload, or null.
  function premiumAccount(id, status) {
    return PREMIUM_ACCOUNT[id] ? PREMIUM_ACCOUNT[id](status) : null;
  }

  function premiumUntil(id, status) {
    return premiumAccount(id, status)?.until || null;
  }

  function remaining(until) {
    return Math.ceil((until - Date.now()) / 86400000);
  }

  // The one wording of a premium expiry: "Premium until DD.MM.YYYY" and its
  // "(N days remaining)" / "(expired)" qualifier.
  function describe(until, tier = 'Premium') {
    const dd = String(until.getDate()).padStart(2, '0');
    const mm = String(until.getMonth() + 1).padStart(2, '0');
    const days = remaining(until);
    return {until: `${tier} until ${dd}.${mm}.${until.getFullYear()}`,
            days: days > 0 ? `(${days} days remaining)` : '(expired)'};
  }

  function line(className, text) {
    const node = document.createElement('span');
    node.className = className;
    node.textContent = text;
    return node;
  }

  // One account alone: the full two-line treatment, absolute date included.
  function fullBlock(name, account) {
    const text = describe(account.until, account.tier);
    const block = document.createElement('span');
    block.className = 'dp-provider-premium-account';
    block.append(line('dp-provider-premium-until', `${name} ${text.until}`),
                 line('dp-provider-premium-days', text.days));
    return block;
  }

  // Several accounts: one compact line each, no absolute date.
  function compactLine(name, account) {
    const days = remaining(account.until);
    return line('dp-provider-premium-account dp-provider-premium-account--compact',
                `${name} ${account.tier} ${days > 0 ? `${days} days remaining` : 'expired'}`);
  }

  /* The row is contextual: one account gets the full block, several get one
   * compact line each, none hides the row (its crown and separator with it).
   * The count it publishes on the row is the one fact the tier headings
   * beneath read (ui-shell-provider-status.css): with no premium account
   * shown there is nothing for a "Premium Services" heading to introduce. */
  function render(entries) {
    const row = document.getElementById('premium-row');
    const label = document.getElementById('lbl-premium');
    if (!row || !label) return;
    const accounts = entries
      .filter(entry => entry.state === 'healthy')
      .map(entry => [entry.name, premiumAccount(entry.id, entry.status)])
      .filter(([, account]) => account);
    const blocks = accounts.length === 1
      ? [fullBlock(...accounts[0])]
      : accounts.map(([name, account]) => compactLine(name, account));
    label.replaceChildren(...blocks);
    row.style.display = blocks.length ? '' : 'none';
    if (blocks.length) row.dataset.premiumAccounts = String(blocks.length);
    else delete row.dataset.premiumAccounts;
  }

  // A provider's own Settings card states the same expiry -- and the same
  // tier -- the same way.
  window.DPPremiumAccount = Object.freeze({until: premiumUntil, account: premiumAccount, describe});

  document.addEventListener('debridpulse:provider-status', event => {
    render(event.detail?.entries || []);
  });
})();
