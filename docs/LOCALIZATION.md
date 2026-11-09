# Localization

DebridPulse 1.0.14 status: **foundation complete (WS0–WS2); WS3a complete;
product localization incomplete.** The runtime, the catalogs, the validators,
the pseudo locales, the per-account language and the coded-error seam exist.
These surfaces read the catalog:

- WS2's proof surfaces: My Account and its Help, the sign-in pages, and the
  account error codes;
- WS3a, the application shell and shared UI: the sidebar (navigation, Provider
  Status, the status rows) and the topbar (processing controls, speed badge
  and cap menu, theme controls); Dashboard; Activity Log; Statistics; and the
  shared owners every page draws with: status badges and failure categories,
  progress and ETA, sizes, speeds and durations, toasts, the pager, search
  notices, storage warnings, premium accounts, source icons, tooltips' tab
  index, and the accessible names the accessibility runtime sets.

Every other surface is still English literals: Downloads, Details and the
download operations (WS3b), Settings (WS3c, WS3d), the server's display
messages (WS3e) and Help (WS4). The census below counts them. No language is
offered: there is no picker, and no target catalog ships.

## 1. Pieces and owners

| Piece | Owner |
|---|---|
| Locale list, names, direction, status, batch | `frontend/static/i18n/manifest.json` |
| Which locale an account may hold (`locale_tags`) and what a target catalog delivers (`delivered`) | `backend/core/localization.py` |
| English catalog (served, messages only) | `frontend/static/i18n/en.json` |
| English metadata (category, translator context, owner, criticality, arguments, geometry) | `frontend/i18n/en.meta.json` (not served) |
| Browser runtime `window.DPI18n` | `frontend/static/ui-i18n.js` (parser-blocking, right after the theme bootstrap) |
| Message formatter | vendored FormatJS `intl-messageformat` 12.1.4, `frontend/static/vendor/intl-messageformat.esm.js`, attached by `ui-i18n-formatter.js` |
| First-paint veil | `frontend/static/ui-i18n.css` (`html[data-dp-locale-pending]`, 4 s failsafe) |
| Catalog asset | `GET /i18n/{tag}.js` (`api.auth_routes.locale_catalog_script`): `window.DPI18nCatalog(tag, messages[, {stale}])`. English's ETag is sha256[:16] of `en.json`; a target's is sha256[:16] of the delivered body |
| Server reads, pseudo, pre-sign-in text, coded errors | `backend/core/localization.py` |
| Each account's language | `account_appearance.locale` (`services.account_appearance.load_locale` / `set_locale`), `GET/PUT /api/account/locale` |
| First-paint projection | the `window.debridPulseLocale=…` line of `/ui-theme-bootstrap.js` |
| Validator | `.github/localization/validate.mjs` |
| Census | `.github/localization/inventory.py` (lexer: `jslex.py`) |

**Messages.** These are ICU MessageFormat with named arguments only, plus `plural`
(with the locale's CLDR categories from `Intl.PluralRules`), `select`, `number` and
`date`. The rich-text tags allowed are `b strong em i code kbd`. The runtime turns
a tag into an element and never parses a translation as markup.

**Keys** are semantic and dotted, named for the concept and never the English
text: `shell.nav.settings`, `account.password.change`,
`error.account.current_password_incorrect`.

**Pages in English.** `index.html` loads the English catalog as
`/i18n/en.js?v=<sha256[:16] of en.json>`. The version must change whenever
`en.json` changes, and `test_localization_catalogs.py` checks that it does.

**Fallback** happens key by key. A target catalog records, for each
translation, the sha256[:16] of the English it was made from (`sources[key]`).
The server delivers a target's current translations only
(`core.localization.delivered`): a translation whose recorded hash no longer
matches the current English is stale and is not sent. The catalog asset names
the stale keys instead (its third argument). So:

- a current translation is shown;
- a stale or missing key is shown in the current English, while every other
  translation in the locale stays;
- `DPI18n.debt()` lists, per locale, the keys shown in English so far as
  `{missing, stale}`.

A target's asset version (its `?v=` and ETag) is the hash of what it delivers.
An English change that makes a translation stale changes the delivered body,
and so the URL and the ETag, even though the target's file did not change; a
cached copy carrying the obsolete translation is never revalidated. The server
reads each catalog file again whenever its modification time or size changes.

A catalog that cannot be read (malformed JSON, `messages` not an object) is
not delivered: its asset is 404, the first-paint projection names no catalog,
and the page is English. A catalog that fails to load in the browser, or does
not arrive within 3 s, leaves the whole page in English in a single switch and
is recorded in `DPI18n.diagnostics()`.

**Which locale an account may hold** is decided in one place,
`core.localization.locale_tags`, for every choice and every stored value:

- English (`canonical`), always;
- a target the manifest marks `supported` that has a readable catalog;
- the pseudo locales, only with `DEBRIDPULSE_I18N_TEST_LOCALES=1`.

A `planned` target is never admitted, and the test setting does not admit one.
Choosing a locale that is not admitted returns 422 and changes nothing. A
stored value that is not (or is no longer) admitted, such as an older build's
planned tag, reads as English and is left as stored. Every target in the
repository is `planned`, so today an account can hold English only. Tests
that need a supported target point `STATIC_DIR` at a synthetic catalog
directory (test doubles, not translations); no production setting admits a
planned locale.

**Pseudo locales** are for tests only and are never offered:

- `en-XA` is accented and about 40% longer, wrapped in `⟦ … ·· ⟧`.
- `ar-XB` is right-to-left (RLO … PDF), with each argument FSI/PDI-isolated.

Both are generated from English: in the browser by an AST transform, and on the
server by `core.localization.pseudo`. The API accepts them only on a runtime
started with `DEBRIDPULSE_I18N_TEST_LOCALES=1`, which admits nothing else.

**Before sign-in** the language is English. No account exists yet, and neither
`Accept-Language` nor the browser's language is read. Sign-in page messages
(`login.*`) use plain `{name}` arguments only and are always HTML-escaped.
`pre_sign_in_locale(explicit)` is the hook a future explicit pre-sign-in choice
would use.

**Errors.** A `DisplayError` keeps its English `detail` exactly as API clients
already receive it, and adds `code` and `params`. The interface shows
`error.<code>` from the catalog and falls back to `detail`.

**Words the server names by a stable identifier.** Some English the interface
shows comes from the server as data beside an identifier. The interface says
it by the identifier, keeping the server's English where the catalog has no
such identifier, so API clients and a newer server are unaffected:

- a transfer's `presentation_label` is a function of its `presentation_status`
  (`status.<presentation_status>`; a consolidation still owed proof, which the
  server marks by `unverified_association_count`, is
  `status.consolidated_unverified`);
- a failed transfer's category (`error.category`) is `error_category.<category>`;
- Provider Status's tier and group headings are `integration.tier.<id>` and
  `integration.group.<id>`.

Provider, integration, palette and plan names are proper names and stay as the
server sends them. Activity Log event messages are the server's journal text
and stay English until WS3e gives them codes.

**Owners.** How a renderer uses the catalog:

- **Static markup** names its key: `data-i18n="key"` for text,
  `data-i18n-attr="attr:key;…"` for attributes, and `data-i18n-values='{"arg": …}'`
  for a message's arguments. `render(document)` writes them before the veil
  lifts, so the first paint is in the page's language.
- **Markup an owner rewrites** (a status row, a count line, the page heading)
  keeps its key for the first paint. The owner removes `data-i18n` when it
  writes its own words, so a later `render()` never resets live state.
- **Every owner re-says its words on `debridpulse:locale-changed`** from the
  state it already holds: no request, no navigation. It keeps focus, open
  panels and dialogs, drafts, scroll, and open `<details>`. A list re-labels
  its rows in place where rebuilding them would lose the reader's place.
- **Figures** come from `DPI18n.number(value, digits)`: fixed decimals, no
  grouping, and the shown language's digits. In English it writes exactly
  what `toFixed` writes. `DPI18n.intlLocale()` names the tag for other `Intl`
  formatters (a pseudo locale formats as English). Counts are plain `{count}`
  arguments; a plural selects on `{count}` and shows `{count}`, never `#`, so
  English digits are unchanged.
- **A message is a whole sentence.** A wording that varies with a count or a
  period is one `plural` or `select` message, never fragments joined in code.
  Shorter phone wordings are separate keys (`…_short`, `…_shortest`), never
  truncations.
- **A formatted message is text.** Owners escape it before markup, and the
  validator refuses literal text that reads as markup.

## 2. Glossary (canonical concepts)

Translate each concept consistently, using one target term per concept. A
`[key]` is the catalog key where the term already appears.

| Concept | Meaning in DebridPulse | Use / don't use |
|---|---|---|
| Transfer | One requested item, from submission through download to completion. | Not "job" or "task" in UI text. |
| Download | The local file materialization of a transfer; also the page that lists transfers [`shell.nav.downloads`]. | |
| Premium service | AllDebrid, Debrid-Link, Premiumize, Real-Debrid, TorBox. | The service names are nonlocalizable. |
| Network source | HTTP & HTTPS, FTP & SFTP, SCP, rsync, WebDAV, Multimeta, Media Downloads. | Protocol names are nonlocalizable. |
| Usenet | The bundled news-server download path. | Nonlocalizable. |
| Account | A person's identity in this installation. | |
| Role: Admin / User / ReadOnly User / Integration [`role.*`] | Fixed role names, shown under the account name. | Translate once and reuse everywhere. Never translate the API's role identifiers. |
| Display Name / Actual Name | What the interface calls the person, and their real name (which is never shown in the sidebar). | Keep the two distinct. |
| My Account [`session.my_account`] | The person's own profile dialog. | |
| Sign in / Log Out [`login.sign_in`, `session.log_out`] | Start and end a browser session. | |
| Easy / Advanced [`presentation.*`] | Per-account presentation. Easy omits listed parts; it never grants anything. | |
| Server / Standalone | Deployment types: many accounts, or one person. | |
| Help & License [`shell.nav.help`] | Product guide and licensing. | Verbatim third-party license texts are nonlocalizable. |
| Activity Log [`shell.nav.activity_log`] | Transfer events, decisions and alerts. | |
| Restore point / backup | A saved copy of configuration and database. | |

**Nonlocalizable**, with the reason for each:

- Product, provider and protocol names, because they are proper names.
- User data: file names, paths, display names and webhook URLs, because they are the user's own.
- Raw provider responses, which are third-party text.
- API identifiers, event codes, data keys and role identifiers, because they are machine contracts.
- The `detail` text of an error, an English API contract kept for API clients. The interface localizes by `code` instead.
- Verbatim third-party legal documents, whose license terms must not be altered.

## 3. Census (WS0) — snapshot at WS3a

Run `python .github/localization/inventory.py --summary` to get the counts, or
add `--json OUT` for the full register. Each entry carries an ID, the English
text, owner and line, context, placeholders, and criticality. The census reads
source, so conditionally rendered variants are all counted.

It is a checked key census:

- markup that names a key (`data-i18n`, `data-i18n-attr`) is keyed, not counted;
- a literal that is a catalog key is a key reference, not counted;
- a string that must never be translated is listed once, with its reason, in
  `.github/localization/nonlocalizable.json` and reported as `exempt`, apart
  from what is still to be keyed. Examples are the product name, CSS class
  lists, the localization runtime's own diagnostic kinds, and developer
  errors that are never shown.

**Correction made in WS3a.** The WS0 census read any plain sentence containing
a full stop as a CSS selector and dropped it. It also missed the compact-wording
attributes (`data-short`, `data-shortest`, `data-placeholder-concise`,
`data-placeholder-minimum`). Corrected, the WS0 baseline is **3,719** frontend
entries (3,038 unique English), not the 3,350 the WS0 packet reported.

| Measure (at WS3a) | Count |
|---|---|
| Frontend literal entries still outside the catalog | 3,118 (2,581 unique English) |
| … rich text / with placeholders | 444 / 234 |
| … critical-warning (destructive, security, data loss) | 231 |
| … long-form Help | 962 |
| Exempt, with reasons (`nonlocalizable.json`) | 29 |
| Server display messages (`detail`) | 669 |
| Catalog keys (English) | 690 |

The largest owners still outside the catalog:

| Owner | Entries | Workstream |
|---|---|---|
| `ui-settings-page.js` | 1,335 | WS3c / WS3d |
| `ui-help-page.js` | 557 | WS4 |
| `ui-settings-help.js` (all topics except My Account) | 405 | WS4 |
| `ui-downloads.js` | 143 | WS3b |
| `app.js` (Details, transfer actions, submission prompts, file upload) | 140 | WS3b |
| `ui-root-provider.js` | 92 | WS3b |

No WS3a owner holds an entry outside the catalog or the exemption register.

**Expansion-risk map.** Under `en-XA` these surfaces need the most care:

- the handheld dock's five 64 px slots, which currently ellipsize;
- every Settings master header, which must stay one row from 320 px;
- the My Account header at 320 px. Its title already wraps to two lines under `en-XA` at 390 px (a WS6 geometry finding);
- table headers and status chips;
- the sidebar's collapsed rail tooltips.

## 4. Translation debt and catch-up

Run `node .github/localization/validate.mjs` (add `--json` for the full report).

**Hard errors** fail with exit 1 (a translation may give a count the English
uses plainly, `{count}`, a `plural` or `number` form where the English
metadata declares that argument a number):

- a non-canonical BCP 47 tag;
- English messages and metadata out of step;
- a non-semantic key;
- metadata missing a category, context, existing owner or criticality;
- an unparsable message;
- positional `{0}` arguments;
- arguments that differ from those declared or from the English source, by name or type;
- tags outside the allowed set, or literal text that reads as markup (`<` then
  a letter, `/`, `!` or `?`);
- plural categories the locale does not have, or a plural without `other`;
- a `login.*` message with anything but plain arguments;
- a translated key that English does not have.

**Debt warnings** exit 0. They list, per locale, the missing keys and the stale
ones. A key is stale when the translation's `sources[key]` (sha256[:16] of the
English it was made from) differs from the current English, the same rule the
server applies before delivering a catalog. The critical-warning keys among the
missing (`criticalMissingKeys`) and among the stale (`criticalStaleKeys`) are
each listed apart.

**`--release`** turns debt into an error for every locale the manifest marks
`supported`. A locale is only ever advertised with zero debt.

**Catch-up workflow.**

1. Run the validator with `--json` and read each locale's `missingKeys`, `staleKeys`, `criticalMissingKeys` and `criticalStaleKeys`.
2. Translate the critical-warning keys first, from the English text and the metadata's translator context.
3. For each translation, record `sources[key]` as the sha256[:16] of the English it was made from.
4. Re-run the validator.

A word that is intentionally the same as English still needs a recorded `sources`
hash. Once recorded, it is no longer debt.

**A feature change that adds text** needs:

- an English message;
- metadata: category, translator context, owner and criticality;
- a new `?v=` on `/i18n/en.js`.

It does not need any target translation: the missing keys show as debt warnings,
not failures.

## 5. Future language-choice integration contract

There is no picker, and this contract proposes no placement. A later choice in
My Account or a first-run step would do the following:

1. **Read.** Call `GET /api/account/locale`, which returns `{locale}`: the
   account's own choice, or the open default Admin's. A stored tag that is
   not admitted (unknown, planned, or a supported one whose catalog is gone)
   reads as `en`, and is left as stored.
2. **Offer.** List the manifest locales whose status is `supported`, naming each
   by its `native` name. Never offer a `planned` or `pseudo` locale; the API
   refuses a planned one in any case.
3. **Choose and persist.** Call `PUT /api/account/locale` with `{locale}`. A
   value that is not admitted (unknown, planned, pseudo without test locales)
   or a foreign field returns 422 and changes nothing. The request is
   authorized for every human role, refused for Integration and anonymous
   callers, and has no target-account field.
4. **Apply.** Call `DPI18n.setLocale(tag, {catalog: '/i18n/<tag>.js?v=<version>',
   dir})`. Owners re-render on `debridpulse:locale-changed`. There is no reload,
   and every unsaved field is kept.
5. **Failure.**
   - If the PUT fails, say so (`error.<code>`, else `detail`) and keep showing
     the language that was last saved.
   - If the catalog does not arrive, the page stays in English, says so once in
     diagnostics, and the saved choice stands.
   - The next cold load reads the choice before first paint.
