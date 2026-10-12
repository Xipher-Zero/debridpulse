/* DPSearch -- THE search lifecycle of the Downloads and Activity Log lists.
 *
 * One controller per list owns that list's search box and its request
 * identity: what the operator is typing, the query committed after a 250 ms
 * pause (or Enter), which request is the newest, the query the shown results
 * answer, and whether that answer covered every eligible record. It never
 * interprets the text -- the server's one search language does
 * (core/search_contract.py) and reports back what it searched and how complete
 * the answer is -- and it never replaces, refills or focuses the input, so
 * typing is never interrupted.
 *
 * Every request a list makes, search-driven or not (a page, a filter, a
 * refresh), takes a ticket here; only the newest ticket's answer may be
 * shown, so a slow or out-of-order response never lands under newer text,
 * filters or pages. A query the server rejects as invalid is not kept:
 * the controller returns to the last query the server accepted and asks the
 * list to show it again, so the results always say what they are for. The
 * list keeps its own filters, order, pages, cursors, selection and rendering.
 *
 * Nothing it does changes the page's geometry: its state is the input's own
 * data-dp-search-state and aria-invalid, and what the operator must be told
 * -- a rejected query, an answer limited to a transfer ID -- is one toast per
 * query, never a line of its own. States: idle (no search), editing (typed,
 * not yet committed), searching, complete, incomplete (the index is still
 * catching up), invalid, unavailable and error.
 */
(function () {
  'use strict';

  const DEBOUNCE_MS = 250;

  function tell(message, kind) {
    try { window.toast(message, kind); } catch (_) {}
  }

  function attach({input, noun, onCommit}) {
    let committed = input.value;
    let accepted = '';
    let latest = 0;
    let timer = null;
    // The state of the shown results, restored when an edit is undone.
    let settled = 'idle';
    // A rejected query keeps its state until the operator types again.
    let rejected = false;
    // How complete the shown answer is, as the server reported it:
    // 'complete', 'index_incomplete' or 'unavailable'.
    let completeness = 'complete';

    function show(state) {
      input.dataset.dpSearchState = state;
      input.setAttribute('aria-invalid', state === 'invalid' ? 'true' : 'false');
    }
    const editing = () => input.dataset.dpSearchState === 'editing';
    function commit() {
      if (timer) { clearTimeout(timer); timer = null; }
      if (input.value === committed) {
        if (editing()) show(settled);
        return;
      }
      committed = input.value;
      latest++;
      show(committed.trim() ? 'searching' : 'idle');
      onCommit();
    }

    show('idle');
    input.addEventListener('input', () => {
      if (timer) clearTimeout(timer);
      rejected = false;
      show('editing');
      timer = setTimeout(commit, DEBOUNCE_MS);
    });
    input.addEventListener('keydown', event => {
      if (event.key === 'Enter') { event.preventDefault(); commit(); }
    });

    return Object.freeze({
      // The query the shown results answer ('' for none).
      accepted: () => accepted,
      // The shown answer's completeness; "no matches" is definitive only
      // when it is 'complete'.
      completeness: () => completeness,
      // A new request identity, carrying the query it sends -- the committed
      // text, never the typing; every older request is superseded.
      request() {
        const ticket = {id: ++latest, query: committed.trim()};
        if (ticket.query && !editing() && !rejected) show('searching');
        return ticket;
      },
      current: ticket => Boolean(ticket) && ticket.id === latest,
      // Supersede every request in flight without starting one.
      invalidate() { latest++; },
      // The newest answer arrived: true when the list should show it.
      settle(ticket, search) {
        if (ticket.id !== latest) return false;
        const fresh = ticket.query !== accepted;
        accepted = ticket.query;
        const meta = search || {};
        completeness = meta.status === 'index_incomplete' || meta.status === 'unavailable' ? meta.status : 'complete';
        if (rejected) settled = 'invalid';
        else if (meta.status === 'index_incomplete') settled = 'incomplete';
        else if (meta.status === 'unavailable') settled = 'unavailable';
        else settled = meta.mode && meta.mode !== 'none' ? 'complete' : 'idle';
        if (fresh && !rejected) {
          if (meta.status === 'unavailable') tell('Text search is unavailable on this installation; only the transfer ID was matched.', 'warn');
          else if (meta.notice) tell(meta.notice, 'info');
        }
        if (!editing()) show(settled);
        return true;
      },
      // The newest request failed: false when it was not the newest. An
      // invalid query is dropped for the last accepted one, which the list is
      // asked to show again ('retried'); the list reports any other failure.
      fail(ticket, error) {
        if (ticket.id !== latest) return false;
        if (error?.status === 400 && error?.detail?.code === 'invalid_query') {
          const message = String(error.detail.message || error.message || 'This search is not valid.');
          tell(`${message} ${accepted ? `Results are still for “${accepted}”.` : `Showing all ${noun}.`}`, 'warn');
          rejected = true;
          committed = accepted;
          latest++;
          if (!editing()) show('invalid');
          onCommit();
          return 'retried';
        }
        if (!editing()) show(error?.status === 503 ? 'unavailable' : 'error');
        return true;
      },
      // Clear the search with the list's other filters (Reset Filters).
      clear() {
        if (timer) { clearTimeout(timer); timer = null; }
        input.value = '';
        committed = '';
        rejected = false;
        latest++;
        show('idle');
      },
    });
  }

  window.DPSearch = Object.freeze({attach});
})();
