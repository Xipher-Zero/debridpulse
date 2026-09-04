/* AllDebrid account-detail adapter.
 * Provider-specific account presentation stays outside the neutral status owner.
 */
(function () {
  'use strict';

  function removeLegacyProviderLink() {
    document.querySelectorAll('.sidebar-footer a[href*="alldebrid.com"]').forEach(link => {
      link.closest('.conn-row')?.remove();
    });
  }

  function hide() {
    const row = document.getElementById('premium-row');
    if (row) row.style.display = 'none';
  }

  function render(status) {
    const row = document.getElementById('premium-row');
    const label = document.getElementById('lbl-premium');
    if (!row || !label || !status?.isPremium) {
      hide();
      return;
    }
    const until = Number(status.premiumUntil || status.premium_until || 0);
    if (!until) {
      hide();
      return;
    }
    const date = new Date(until * 1000);
    const dd = String(date.getDate()).padStart(2, '0');
    const mm = String(date.getMonth() + 1).padStart(2, '0');
    const yyyy = date.getFullYear();
    const days = Math.ceil((date - Date.now()) / 86400000);
    const remaining = days > 0 ? `(${days} days remaining)` : '(expired)';
    label.replaceChildren();
    const untilNode = document.createElement('span');
    untilNode.className = 'dp-provider-premium-until';
    untilNode.textContent = `AllDebrid Premium until ${dd}.${mm}.${yyyy}`;
    const daysNode = document.createElement('span');
    daysNode.className = 'dp-provider-premium-days';
    daysNode.textContent = remaining;
    label.append(untilNode, daysNode);
    row.style.display = '';
  }

  removeLegacyProviderLink();
  document.addEventListener('debridpulse:provider-status', event => {
    const entry = (event.detail?.entries || []).find(candidate => candidate.id === 'alldebrid');
    if (!entry || entry.state !== 'healthy') {
      hide();
      return;
    }
    render(entry.status);
  });
})();
