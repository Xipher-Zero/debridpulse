# Localization: operating guide, readiness matrix and release decisions (1.0.14)

This is the standalone guide for working on DebridPulse's interface languages, and the record of what
the 1.0.14 localization work proved and did not prove. The reference for the pieces themselves (runtime,
catalogs, metadata, validator rules, census, glossary and the future language-choice contract) is
[`LOCALIZATION.md`](LOCALIZATION.md); this file does not repeat it.

**Status at WS7-B (2026-11-11).**
- All 18 target catalogs are complete: 3467 of 3467 messages each, current against the English, and
  reviewed.
- Every target is still **`planned`**. No language is offered, there is no language picker, and nothing
  was activated, tagged or released.
- Every translation and review is **AI-made**. No human or native-speaker review has taken place.

## 1. Product rules that govern localization

These are the product owner's decisions (2026-11-11) and bind every later change:

1. **Localization is best effort. Text fits the existing interface; the interface is never changed to
   fit a language.** No CSS, markup, typography, geometry, breakpoint or application-JavaScript change is
   made to accommodate a translation. A translation that does not fit is shortened in the catalog, never
   the interface widened.
2. **Complete translations and correct meaning are still required.** A language is never advertised with
   missing, stale or unreviewed messages.
3. **Recognizable, unambiguous abbreviations** are allowed where no natural complete word fits (fr
   «Téléch.», uk «Завант.»). A word that narrows the meaning (for example "downloaded" for a page that also
   lists queued downloads) is not.
4. **Language-specific typography and mixed-direction rendering limits may be accepted and documented**
   without a corrective implementation. Arabic plain-text bidi (section 6, A1) is accepted on that basis.
5. **Stored values, technical literals and copied text are never altered** for display: no bidi control
   characters in catalogs, values or copied content.
6. **An accepted limitation is never described as qualified or defect-free.**

## 2. Working on languages

### Adding or changing English text (every feature change)

1. Add or change the message in `frontend/static/i18n/en.json`, with its metadata in
   `frontend/i18n/en.meta.json`: category, translator context, owner and criticality. Give a geometry
   note where the space is tight.
2. Give `/i18n/en.js` in `frontend/static/index.html` a new `?v=`.
3. Run `node .github/localization/validate.mjs`. Hard errors must be fixed before merging.
4. No target translation is needed in the same change. A new or changed English message makes that key
   **debt** in every target. Until it is translated again, the page shows English **for that key alone**,
   and the validator lists it as a warning.

### Translating, or catching up after English changed

1. Run `node .github/localization/validate.mjs --json` and read each locale's `missingKeys` and
   `staleKeys`. Take `criticalMissingKeys` and `criticalStaleKeys` first.
2. Translate from the English and its translator context. Use the locale's termbase
   (`frontend/i18n/terms/<tag>.json`) for every concept it names, and keep every protected name and
   literal exactly (`terms/en.json` `protected`).
3. Record `sources[key]` in the catalog: the sha256[:16] of the English the translation was made from.
4. Record the review in the locale's ledger (`frontend/i18n/review/<tag>.json`). Each entry holds
   `source` and `translation` (the hashes of what was reviewed) and `provenance` (`ai-review` or
   `human-review`).
   - A **critical-warning** key also needs a back-translation made without seeing the English
     (`ai-back-translation` or `human-back-translation`, with its text), compared with the English by
     someone else.
   - A rendering intentionally identical to English needs an `identical` reason.
   - A human step names its reviewer and evidence.
5. Run `node .github/localization/validate.mjs --complete=<tag>`. It must exit 0: zero debt, a term for
   every concept, and no unreviewed text or open finding.
6. **If a page or control name changes, everything that names it follows.** That means:
   - the nav label (`shell.nav.*`) and page title (`shell.page.*.title`);
   - the termbase term;
   - every Help bold path whose segment names that control (the validator's label-identity check fails
     otherwise);
   - Help prose that refers to the page by name. Generic uses of the same word stay as they are.

   A word can name two different controls; for example the fr and uk Downloads page and the Downloads
   tab inside Settings. Rename only the one that changed.
7. **Fit check at 320 px.** The phone dock label has about 62 px at 10 px type, and the header title
   about 81 px after the header's yields. Measure a changed page name in the running interface (as
   `localization-foundation.spec.js` L24b/L24c do) before staging it.

### Adding a new language

1. Add the tag to `frontend/static/i18n/manifest.json` as `planned`, with its `native` name and `dir`.
2. Write its termbase, a catalog with every key, and a review ledger, as above.
3. Add it to `targets` in `backend/tests/test_localization_catalogs.py`, so CI requires it to stay
   complete.
4. Qualify it with `--complete=<tag>`, the locale probe (`DP_LOCALE=<tag> npx playwright test
   localization-locale.spec.js`) and a 320 px fit check.
5. Making it `supported` (offered to accounts) is a separate, explicit product decision (section 7).

### Diagnosing debt or a broken catalog

| What you see | Where to look | What it means |
|---|---|---|
| Some words are English in an otherwise translated page | `DPI18n.debt()` in the browser; the validator's `missingKeys` and `staleKeys` | Those keys are missing or stale; each falls back to English alone |
| The whole page stays English and says so once in diagnostics | Server log line `Locale catalog <tag> is unreadable and is not delivered`; `/i18n/<tag>.js` returns 404 | The catalog file is missing or not valid JSON. The interface stays English, and a `supported` locale whose catalog is unreadable is no longer admitted |
| `--complete` / `--release` / the CI catalog test fail | The validator's `ERROR` lines, which name the locale and key | Missing, stale, unreviewed or un-back-translated keys, an open finding, or a broken message |

## 3. Validation and CI

**Local:** `node .github/localization/validate.mjs [--json] [--complete=<tag>] [--release]`. The hard
errors, debt warnings and review checks are listed in `LOCALIZATION.md` §4.

**In CI.** The required **Tests** workflow runs
`backend/tests/test_localization_catalogs.py` in its contract layer. The workflow fails a layer if any
test is skipped, and this module is skipped only when Node is missing.
- **Hard errors fail CI.** `test_the_repository_catalogs_validate_and_report_their_translation_debt`
  asserts that the validator exits 0, that **all 18 targets** have a catalog, termbase and ledger, are
  `planned`, have zero missing, stale, unreviewed and un-back-translated keys and no open finding, and
  that each passes `--complete=<tag>`.
- **Planned catalogs therefore cannot silently lose completeness.** `--release` checks only `supported`
  locales; the CI test checks all 18.
- **Other tests in the module** cover each class of broken message, the census of first-party literals
  outside the catalog, the shell's keys, the sign-in page, the pseudo locales and the catalog asset's
  versioning.
- **Proved on 2026-11-11 (WS7-B):**
  - the test ran and passed in CI on `13da60c4` (Tests job 130676889665);
  - run locally, the same test passes on the repository and **fails** on a copy with one French message
    removed. The failure names the key («fr shell.nav.settings: a source hash without its translation»).
- **Caveat.** Tests has been red on `1.0.14` for reasons unrelated to localization (section 6, U1). A
  localization failure would block Release Promotion, but it is not visible on its own while those
  failures remain.

## 4. Runtime behaviour: delivery, fallback and failure

| Behaviour | How it works | Evidence |
|---|---|---|
| What an account downloads | `ui-theme-bootstrap.js` states the account's language before first paint. The page loads `/i18n/en.js` (always: English is every key's fallback) and, for another language, `/i18n/<tag>.js?v=<content hash>` | `backend/api/auth_routes.py` `_locale`, `locale_catalog_script` |
| Cost on the wire (measured 2026-11-11, uncompressed) | `en.js` 370 KB; a target 363 KB (zh-Hans) to 763 KB (hi). One account's first load is 0.73–1.13 MB. Gzip would be about 92 KB plus 94–126 KB | `ws7b` packet `delivery.txt` |
| Repeat loads | `Cache-Control: no-cache, must-revalidate` with a content `ETag`: a revalidation answers **304**, so the bytes are paid once per catalog version | Same |
| A message missing or stale in a catalog | The server withholds a stale translation (made from older English). The interface shows English for that key alone and counts it as debt | `test_account_locale.py` (`…withheld_alone…`); browser L07b |
| A missing, corrupt or invalid catalog | Not delivered (404, logged). The page stays English and says so once; a `supported` locale with an unreadable catalog is not admitted | `test_account_locale.py` (unreadable / malformed cases); L07b |
| A catalog fetch that fails or never arrives | The page stays English, says so once in diagnostics, and the saved choice stands | L07b |
| Localized start-up in test mode | `DEBRIDPULSE_I18N_TEST_LOCALES=1` admits the pseudo locales (`en-XA`, `ar-XB`). The stored locale is applied at first paint (`lang`, `dir`) | L02, L03; WS6-C first-paint evidence |

**Pseudo locales** (`en-XA` accented and about 40% longer, `ar-XB` right-to-left) exist only with
`DEBRIDPULSE_I18N_TEST_LOCALES=1`. They are the supported way to exercise localization without
activating a language.

## 5. Boundaries that a language never changes

A language changes words only. These are verified by existing tests:

| Boundary | Evidence |
|---|---|
| Easy presentation keeps Advanced-only items omitted in every language | L08a |
| Roles: every human role owns its language; Integration, anonymous and crossed sessions own none. A language choice changes the language only, never a role's controls | `test_account_locale.py` (`…every_human_role_owns_one…`, `…changes_the_language_only`) |
| Deployment: the open default Admin owns the language in Server and Standalone | `test_account_locale.py` (`…across_deployment_types`) |
| Admission: production admits English and `supported` targets only. A `planned`, unknown or foreign value is refused with **422** and changes nothing; a stored tag that is no longer admitted reads as English and stays stored | `test_account_locale.py` (`…never_a_planned_one`, `…refused_and_changes_nothing`, `…reads_as_english_and_stays_stored`); live probe 2026-11-11: `PUT /api/account/locale {"locale":"de"}` returned 422 `appearance.locale_unknown` |
| A preference that cannot be read in time is never shown as the saved choice: the page is English and says nothing about the choice | `test_account_locale.py` (`…an_unanswered_read_as_unknown`) |

**Not exercised.** A failed save through a **future production language picker** does not exist yet. The
refusal above is the API contract such a picker would rely on, not a test of the picker (§7).

## 6. Release-decision ledger

### Verified release requirements

| # | Requirement | Evidence |
|---|---|---|
| V1 | All 18 catalogs are complete and current: 3467/3467 messages, 0 missing, 0 stale, 0 extra | WS7-B reconciliation (`reconciliation.txt`); CI catalog test |
| V2 | Every message is reviewed for the current English and current text. All 251 critical keys are back-translated and compared in every language | Same (`reviewedCurrent` 3467, critical 251/251, ×18) |
| V3 | Every termbase covers all 31 concepts; 0 open findings; every rendering identical to English has a recorded reason | Same |
| V4 | The release check refuses missing, stale and un-back-translated critical text, with per-key diagnostics | WS7-B isolated copy with all 18 `supported`: passes unchanged; refuses each of three broken copies |
| V5 | No 320 px truncation of page titles (5 pages) or dock labels in any of the 19 languages (Chromium, host fonts) | WS7-A r2 census; L24b, L24c |
| V6 | Dialogs at 320–1440 px (geometry, accessible name and description, focus trap and restoration) in ar, ja and de | WS6-C (108/108) |
| V7 | Right-to-left layout and navigator, and isolation of technical literals in rich text | WS6-A (L03, L03b, L03c) |
| V8 | Fallback, failure and admission behaviour (§4, §5) | Existing tests, re-run 2026-11-11: `test_account_locale.py` 26 passed; L03, L07b, L08a passed |

### Corrected release blockers

| # | Blocker | Correction |
|---|---|---|
| C1 | 320 px dock and title truncation in fr, de, es, pt-BR, uk and it | WS7-A (`474923b1`), text only: fr Aperçu and Téléch.; de Optionen; es and pt-BR Ajustes; uk Параметри and Завант.; it Opzioni |
| C2 | Help and on-screen text still named renamed pages by their old name (fr «Tableau de bord»; Settings page references in de, es, pt-BR, uk, it) | WS7-B, text only: 162 messages (fr 9, de 33, es 35, pt-BR 35, uk 16, it 34). Each was classified as a page reference against the English and independently reviewed; 9 corrections were confirmed; all 25 critical messages were back-translated blind and found equivalent. Generic uses of the word ("settings" values, card names) are unchanged |
| C3 | Open dialogs kept the old language on a language change | WS6-C (`13da60c4`) |
| C4 | Right-to-left section navigation and literal isolation | WS6-A (`2bcbe66c`) |
| C5 | Japanese and Arabic dock and title cut; Japanese and Korean in-word line breaks | WS6-B (`a7139597`) |

### Known limitations accepted by the product owner

| # | Limitation | Basis |
|---|---|---|
| A1 | **Arabic plain-text bidi.** Values inserted into plain-text messages (toasts, tooltips, attributes) are not direction-isolated, so punctuation next to a left-to-right value can display out of place. Rich text is isolated (WS6-A). Stored and copied values are unaffected | Localization Support Policy Amendment, 2026-11-11 |
| A2 | A toast already on screen keeps its language after a language change; new messages use the new language | WS7 product decisions |
| A3 | Japanese `word-break: auto-phrase` is Chromium-only; other engines keep `line-break: strict` | Same |
| A4 | Japanese and Arabic dock and title fits have zero margin in Chromium with Noto fonts; other fonts and devices are not verified | Same |
| A5 | Screen-reader output is not qualified; accessible names and roles are verified from the DOM only | Same |
| A6 | zh-Hant uses Taiwan terminology; Hong Kong usage is out of scope | Same |
| A7 | German «Passwort entfernen» for Clear Password | Same |
| A8 | fr «Téléch.» and uk «Завант.» are abbreviations wherever the Downloads page is named in the sidebar, header and Help paths; Help prose keeps the full word as its expansion | WS7-A HOLD decision |
| A9 | All translation and review is AI-only, with no human or native-speaker review | Recorded in every ledger |

### Unresolved blockers

| # | Blocker | Owner and consequence |
|---|---|---|
| U1 | **Tests and the security job are red on `1.0.14`, for reasons unrelated to localization.** No Tests run has passed in the last 40, and Release Promotion requires Tests. On `13da60c4` there were 16 failing UI-contract tests (classified ANCHOR_REPRODUCED_FLAKE or INCONCLUSIVE), plus the security job's dependency CVEs | Not localization. It blocks any 1.0.14 release, and it hides a localization CI failure (§3) until it is fixed |
| U2 | **Catalogs are served uncompressed** (§4): 0.73–1.13 MB on an account's first load, against about 0.2 MB with gzip | Changing it is an application change, which WS7 is not authorized to make. Options: accept it (paid once per version; often compressed by a reverse proxy), or authorize response compression separately |
| U3 | **125 Help spans name a control that has no exact catalog key** (14 are the Help manual's own tab names), plus 1 deliberately generic «Clear». The label-identity check cannot verify these; only per-message review covers them | Recorded per span in `frontend/i18n/en.labels.json`. Accept as review-only, or map them to keys in a later English-source change |

### Activation-dependent requirements not yet exercised

| # | Requirement | Why not yet |
|---|---|---|
| D1 | First paint with a real translated catalog (veil until it arrives, no English flash) | Needs a `supported` language; only the pseudo-locale path is qualified |
| D2 | The production language choice: picker, persistence and switching lifecycle (the contract is in `LOCALIZATION.md` §5) | No picker exists or is authorized |
| D3 | A failed save through that picker | The API refusal is tested; the picker's handling is not |
| D4 | Real-device and real-font fits (iOS, Android, Safari, Firefox) | Only Chromium with host fonts was measured |

## 7. Activation and rollback

- **Activating a language is a separate product decision.** It means changing that tag's manifest status
  from `planned` to `supported`. Before doing so:
  - `node .github/localization/validate.mjs --release` must exit 0 with that manifest;
  - the activation-dependent items D1–D4 must be qualified on the release candidate;
  - this ledger must be updated.
- **Rollback** means setting the status back to `planned`. Accounts that chose the language read it as
  English, and their stored choice is kept, so re-activating restores it. Removing or corrupting a catalog
  has the same effect: the language is withdrawn, never half-shown.
- **Release qualification** recomputes §6 on the tagged candidate. Zero debt is required for every
  advertised language, and any withdrawal of a contemplated language is an explicit, owner-approved
  decision.

## 8. Per-language readiness matrix

Every target: 3467/3467 messages, current and reviewed; 251/251 critical keys back-translated; termbase
31/31; `--complete` passes; no 320 px page-title or dock truncation (Chromium); status `planned`; AI-only
review (A9). Activation also needs D1–D4 in every case.

| Language | Script and direction | Notes beyond the common state | Ready for an activation decision? |
|---|---|---|---|
| de German | Latin, LTR | Settings page «Optionen» (WS7-A); A7 | Yes, subject to A9 and D1–D4 |
| fr French | Latin, LTR | «Aperçu» and «Téléch.» (A8) | Yes, subject to A8, A9, D1–D4 |
| es Spanish | Latin, LTR | Settings page «Ajustes» | Yes, subject to A9, D1–D4 |
| it Italian | Latin, LTR | Settings page «Opzioni» | Yes, subject to A9, D1–D4 |
| pt-BR Portuguese (Brazil) | Latin, LTR | Settings page «Ajustes» | Yes, subject to A9, D1–D4 |
| pt-PT Portuguese (Portugal) | Latin, LTR | Independent of pt-BR | Yes, subject to A9, D1–D4 |
| nl Dutch | Latin, LTR | — | Yes, subject to A9, D1–D4 |
| pl Polish | Latin, LTR | few/many plurals | Yes, subject to A9, D1–D4 |
| ru Russian | Cyrillic, LTR | few/many plurals | Yes, subject to A9, D1–D4 |
| uk Ukrainian | Cyrillic, LTR | «Параметри» and «Завант.» (A8); few/many plurals | Yes, subject to A8, A9, D1–D4 |
| tr Turkish | Latin, LTR | Dotted and dotless i kept distinct | Yes, subject to A9, D1–D4 |
| id Indonesian | Latin, LTR | — | Yes, subject to A9, D1–D4 |
| hi Hindi | Devanagari, LTR | Largest catalog (763 KB uncompressed, U2) | Yes, subject to A9, D1–D4 |
| ja Japanese | CJK, LTR | A3 and A4 (zero-margin fits) | Yes, subject to A3, A4, A9, D1–D4 |
| ko Korean | Hangul, LTR | `keep-all` word breaking | Yes, subject to A9, D1–D4 |
| zh-Hans Chinese (Simplified) | CJK, LTR | Mainland usage | Yes, subject to A9, D1–D4 |
| zh-Hant Chinese (Traditional) | CJK, LTR | Taiwan usage (A6) | Yes, subject to A6, A9, D1–D4 |
| ar Arabic | Arabic, **RTL** | A1 (plain-text bidi) and A4 (zero-margin dock) | Yes, subject to A1, A4, A9, D1–D4 |

**No language is declared production-ready.** U1 blocks any 1.0.14 release regardless of language. D1–D4
are unqualified, and every translation is AI-only.
