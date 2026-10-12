/* DPSearch -- THE search lifecycle of the Downloads and Activity Log lists.
 *
 * One controller per list owns that list's search box and its request
 * identity: what the operator is typing, the query committed after a 250 ms
 * pause (or Enter), which request is the newest, the query the shown results
 * answer, and the search status line. It never interprets the text -- the
 * server's one search language does (core/search_contract.py) and reports
 * back what it searched and how complete the answer is -- and it never
 * replaces, refills or focuses the input, so typing is never interrupted.
 *
 * Every request a list makes, search-driven or not (a page, a filter, a
 * refresh), takes a ticket here; only the newest ticket's answer may be
 * shown, so a slow or out-of-order response never lands under newer text,
 * filters or pages. A query the server rejects as invalid is not kept:
 * the controller returns to the last query the server accepted and asks the
 * list to show it again, so the results always say what they are for. The
 * list keeps its own filters, order, pages, cursors, selection and rendering.
 *
 * States: idle (no search), editing (typed, not yet committed), searching,
 * complete, incomplete (the index is still catching up), invalid, unavailable
 * and error.
 */
(function () {
  'use strict';

  const DEBOUNCE_MS = 250;

  function attach({input, status, noun, onCommit}) {
    let committed = input.value;
    let accepted = '';
    let latest = 0;
    let timer = null;
    let state = 'idle';
    // What the line said for the shown results, restored when an edit is undone.
    let settled = ['idle', ''];
    // A rejected query's explanation, kept until the operator types again.
    let rejected = '';
    // Whether the shown answer covered every eligible record.
    let proven = true;

    function show(next, text) {
      state = next;
      input.dataset.dpSearchState = next;
      input.setAttribute('aria-invalid', next === 'invalid' ? 'true' : 'false');
      if (!status) return;
      status.textContent = text || '';
      status.hidden = !text;
    }
    function commit() {
      if (timer) { clearTimeout(timer); timer = null; }
      if (input.value === committed) {
        if (state === 'editing') show(...settled);
        return;
      }
      committed = input.value;
      latest++;
      show(committed.trim() ? 'searching' : 'idle', committed.trim() ? 'Searching…' : '');
      onCommit();
    }

    input.addEventListener('input', () => {
      if (timer) clearTimeout(timer);
      rejected = '';
      show('editing', '');
      timer = setTimeout(commit, DEBOUNCE_MS);
    });
    input.addEventListener('keydown', event => {
      if (event.key === 'Enter') { event.preventDefault(); commit(); }
    });

    return Object.freeze({
      // The query the shown results answer ('' for none).
      accepted: () => accepted,
      // Whether "no matches" in the shown answer is definitive.
      proven: () => proven,
      // A new request identity, carrying the query it sends -- the committed
      // text, never the typing; every older request is superseded.
      request() {
        const ticket = {id: ++latest, query: committed.trim()};
        if (ticket.query && state !== 'editing' && !rejected) show('searching', 'Searching…');
        return ticket;
      },
      current: ticket => Boolean(ticket) && ticket.id === latest,
      // Supersede every request in flight without starting one.
      invalidate() { latest++; },
      // The newest answer arrived: true when the list should show it.
      settle(ticket, search) {
        if (ticket.id !== latest) return false;
        accepted = ticket.query;
        const meta = search || {};
        proven = meta.status !== 'index_incomplete' && meta.status !== 'unavailable';
        if (rejected) {
          settled = ['invalid', `${rejected} ${accepted ? `Results below are for “${accepted}”.` : `Showing all ${noun}.`}`];
        } else if (meta.status === 'index_incomplete') {
          const pending = Number(meta.pending) || 0;
          settled = ['incomplete', `Recent ${noun} are still being indexed (${pending > 2000 ? 'more than 2000' : pending}); `
            + 'matches among them may be missing. Try again shortly.'];
        } else if (meta.status === 'unavailable') {
          settled = ['unavailable', 'Text search is unavailable on this installation; only the transfer ID was matched.'];
        } else {
          settled = [meta.mode && meta.mode !== 'none' ? 'complete' : 'idle', meta.notice || ''];
        }
        if (state !== 'editing') show(...settled);
        return true;
      },
      // The newest request failed: false when it was not the newest. An
      // invalid query is dropped for the last accepted one, which the list is
      // asked to show again ('retried'); the list reports any other failure.
      fail(ticket, error) {
        if (ticket.id !== latest) return false;
        const message = String(error?.detail?.message || error?.message || 'Search failed.');
        if (error?.status === 400 && error?.detail?.code === 'invalid_query') {
          rejected = message;
          committed = accepted;
          latest++;
          if (state !== 'editing') show('invalid', message);
          onCommit();
          return 'retried';
        } else if (state !== 'editing' && error?.status === 503 && error?.detail?.code === 'search_unavailable') {
          show('unavailable', message);
        } else if (state !== 'editing' && ticket.query) {
          show('error', `The search could not be completed: ${message}`);
        }
        return true;
      },
      // Clear the search with the list's other filters (Reset Filters).
      clear() {
        if (timer) { clearTimeout(timer); timer = null; }
        input.value = '';
        committed = '';
        rejected = '';
        latest++;
        show('idle', '');
      },
    });
  }

  window.DPSearch = Object.freeze({attach});
})();
