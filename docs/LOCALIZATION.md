# Localization

DebridPulse 1.0.14 status: **foundation complete (WS0–WS2); WS3a–WS3e and
WS4 (WS4a, WS4b) complete; product localization incomplete.** The runtime, the catalogs, the validators,
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
  index, and the accessible names the accessibility runtime sets;
- WS3b, the download operations: Downloads (headings, filters, search, the
  date menu, summaries, row and bulk actions, queue order, removal and
  restart dialogs); Details (facts, route history, original resource, error
  disclosure, withdrawal, trace, the per-file candidate disclosure); file
  selection; provider switching (`ui-root-provider.js`) and source switching
  (`ui-group-candidates.js`, its discard confirmation included); the
  authentication-required dialog; the server folder picker; transfer uploads;
  and the submission prompts (local network, acquisition scope);
- WS3c, Settings services and engines: the Settings shell (tabs, navigator,
  header identity and its shorter forms, disclosures, help chips' names);
  Services (premium provider cards and their keys, account connections and
  tuning; Usenet and its news-server cards; Network Sources); Downloads
  (behavior and limits, the transfer-method tuning cards, disk space and
  recovery, Download Engine Activity); Extraction and the archive-password
  editor; and their dialogs, busy labels, refusals and results;
- WS3d, Settings administration and integrations: Authentication (Status
  figures and their shorter forms, Username & Password, sessions, OpenID
  Connect and its verification window); Users & Access (Deployment Type and its
  switch confirmations, User Accounts, roles and sign-in methods, Edit User,
  Merge Accounts, the account-impact confirmations, Integration Accounts and
  their tokens, Integration Delivery Profiles, Account Details and Activity);
  Notifications (webhooks, avatar, events, statistics reports, a User's own
  Notifications page); Data & Maintenance (Event Logging, Backups & Retention,
  the Backups manager and restore, Database Reset); a refused field value's
  words (`ui-settings-persistence.js`); and the shared Settings dialog's
  default slot words, typed-phrase label and progress lines.

- WS3e, the server's refusals: every message the server returns for display
  is named by a stable code (or is exempt, with the reason its words never
  become interface text); the interface says a coded refusal in the reader's
  language and keeps the English `detail` for anything else (§1, Errors).

- WS4a, the Field Manual (the Help page, `ui-help-page.js`): every section,
  passage, step, accordion, inset, heading and accessible name, for every
  reader -- each role, deployment type and Easy or Advanced -- the License
  viewer's own words (`ui-help-license-documents.js`; the bundled documents
  stay as bundled), and the shared section navigator's tooltip
  (`ui-section-nav.js`).

- WS4b, Settings Help (`ui-settings-help.js`): every topic a Settings card,
  the Role selectors or My Account opens -- titles, headings, paragraphs and
  list items, for every reader -- and the role comparison table, its caption,
  headings and answers.

The census finds no first-party interface literal outside the catalog or the
reviewed exemptions. No language is offered: there is no picker, and no target
catalog is offered (WS5).

WS5-0 adds the translation tooling a language candidate is qualified with (§4,
WS5 translation checks): the concept register and protected names, termbases and
review ledgers, the Help label map, the review sheet and the locale probe. Every
target stays `planned`.

WS5-A1 adds the German catalog (`frontend/static/i18n/de.json`, every key), its
termbase (`frontend/i18n/terms/de.json`) and review ledger
(`frontend/i18n/review/de.json`). It is AI-generated and independently AI-reviewed,
and every critical warning was back-translated and compared with the English
(the ledger records each step). No human or native review has taken place.
German passes `validate.mjs --complete=de` and stays `planned`: its catalog is
served as a development asset, but no account can choose it until a separate
activation after WS6.

WS5-A2 adds French, Spanish and Italian the same way, each with its own catalog
(`frontend/static/i18n/{fr,es,it}.json`), termbase and review ledger: AI-generated,
independently AI-reviewed, every critical warning back-translated and compared, no
human or native review. Each passes `validate.mjs --complete=<tag>` on its own and
stays `planned`.

WS5-A3 adds Brazilian Portuguese, European Portuguese and Dutch the same way
(`frontend/static/i18n/{pt-BR,pt-PT,nl}.json`, each with its own termbase and review
ledger). The two Portuguese catalogs are separate regional translations from the
English, not copies of each other: each has its own termbase (for example Configurações /
Definições, Salvar / Guardar, Excluir / Eliminar, the Brazilian gerund versus *a* +
infinitive in Portugal). Each passes `validate.mjs --complete=<tag>` on its own and stays
`planned`.

WS5-B1 adds Polish, Russian and Ukrainian the same way
(`frontend/static/i18n/{pl,ru,uk}.json`, each with its own termbase and review ledger),
the first catalogs whose plurals use the CLDR *few* and *many* categories. Russian and
Ukrainian are independent translations from the English, neither a variant of the other:
each has its own termbase (for example Настройки / Налаштування, Загрузка / Завантаження,
Учётная запись / Обліковий запис), and an AI review of the pair confirmed it. Each passes
`validate.mjs --complete=<tag>` on its own and stays `planned`.

WS5-B2 adds Turkish, Indonesian and Hindi the same way
(`frontend/static/i18n/{tr,id,hi}.json`, each with its own termbase and review ledger).
Turkish keeps dotted İ/i and dotless I/ı distinct and never attaches a suffix to a
placeholder; Indonesian is standard Indonesian (not Malay) with plurals in the single
`other` category; Hindi is NFC Devanagari, where the `one` category covers both 0 and 1.
Each passes `validate.mjs --complete=<tag>` on its own and stays `planned`.

WS5-C1 adds Japanese and Korean the same way (`frontend/static/i18n/{ja,ko}.json`, each
with its own termbase and review ledger). Both use the single CLDR `other` plural
category, with counters (件 / 개) after numbers; Japanese prose uses the polite です・ます
form with full-width punctuation, Korean prose the formal polite style with standard
spacing and dual particles after placeholders. Each passes `validate.mjs --complete=<tag>`
on its own and stays `planned`.

WS5-C2 adds Simplified and Traditional Chinese (`frontend/static/i18n/{zh-Hans,zh-Hant}.json`,
each with its own termbase and review ledger). The two are translated and reviewed
independently from English, never converted from one script to the other: zh-Hans follows
mainland software usage (服务器, 用户, 文件夹, “ ” quotes), zh-Hant follows Taiwan software
usage (伺服器, 使用者, 資料夾, 「」 quotes). Both use the single CLDR `other` plural category
with measure words after numbers. Each passes `validate.mjs --complete=<tag>` on its own and
stays `planned`.

WS5-D adds Arabic (`frontend/static/i18n/ar.json`, with its own termbase and review ledger):
Modern Standard Arabic with all six CLDR plural categories (`zero`, `one`, `two`, `few`,
`many`, `other`) wherever English varies by count, Arabic punctuation (، ؛ ؟) and « » quotes,
Western digits, and Latin-script technical literals kept intact. The catalog carries no
Unicode bidi control characters; page direction comes from the manifest (`dir: rtl`), and RTL
layout and bidi isolation are WS6 qualification work. It passes `validate.mjs --complete=ar`
and stays `planned`.

Progress: all 18 production targets have complete, reviewed catalogs (de, fr, es, it, pt-BR,
pt-PT, nl, pl, ru, uk, tr, id, hi, ja, ko, zh-Hans, zh-Hant, ar), all still `planned`; none is
activated. Responsive/RTL qualification (WS6) and maintainability (WS7) remain.

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
| Validator | `.github/localization/validate.mjs` (sample rendering shared with the review sheet: `messages.mjs`) |
| Concept register and protected names/literals (English) | `frontend/i18n/terms/en.json` (not served) |
| A locale's termbase | `frontend/i18n/terms/<tag>.json` (not served) |
| A locale's review ledger (provenance, back-translations, findings, recorded exceptions) | `frontend/i18n/review/<tag>.json` (not served) |
| Help label map (every `<b>` span of `help.*`, classified) | `frontend/i18n/en.labels.json` (not served) |
| Bilingual review sheet | `.github/localization/review_sheet.mjs` |
| Locale browser probe | `frontend/browser/localization-locale.spec.js` (`DP_LOCALE`, default `en-XA`) |
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

**Errors (WS3e).** The contract API clients keep is unchanged:

- the HTTP status;
- the English `detail`;
- the established fields (a structured refusal's `code`, `reason`,
  `confirmation`, figures);
- the headers.

Everything below is additive.

- **Named at the raise.** A refusal is named where its condition is known,
  never from its English:
  - `DisplayError(status, detail, code=…, params=…)`;
  - a `Coded` exception: `CodedValueError` / `CodedRuntimeError`, or a domain
    refusal that mixes `Coded` in (`AccountRefused`, `NotificationRefused`,
    `InvalidSearch`, `InvalidNzb`, `StagedInputError`, `BackupRejected`,
    `RestoreFailed`, `ConfigurationRefused`…). Its type, and every `except`
    that catches it, are unchanged.
- **Kept at the boundary.** `refusal(status, detail, cause)` is the one
  boundary that turns an exception into a response. It keeps `detail` exactly
  and carries the cause's code. A cause that names none is marked
  `unclassified`: the explicit fallback, which claims nothing.
- **A wrapped cause.** A refusal that wraps a coded cause names it as a
  parameter `{"$t": "error.<code>", "$v": {…}}` (`said`), and the interface
  says that from its own code.
- **The body.** A JSON refusal is `{"detail", "code", "params"}`.
- **Structured refusals.** A structured refusal keeps its machine `code`
  (`invalid_query`, `stale_revision`…) and names its words by `message_code` +
  `params`.
- **Field refusals.** A refused field (400/422) names its rule: `code:
  "validation.<rule>"`, `params` = the rule's bounds, never the refused value.
  The interface says `error.validation.<rule>`, or the bare
  `error.requirement.<rule>` after the field's name (`ui-settings-persistence.js`).
- **Transfer refusals.** A transfer refusal (`TransferError`) keeps its
  structured `error`. The interface says `error.category.<category>`, whose
  English is exactly the server's `detail`.
- **Parameters.** A parameter never discloses more than the sanitized detail
  the response sends.
  - It is a scalar. Text passes the same canonical sanitizer as every API
    detail (`core.logging_utils.sanitize_log_value`), a coded cause's included,
    while numbers and booleans keep their types.
  - Secret-named parameters are refused.
  - Text is admitted by **authority**, never by content. A text value is sent
    only when its producer made it a `Fact`: a server-established value, such
    as a figure, a schema field or bound, an identifier from a closed set it
    recognises (`known`, e.g. `AcquisitionScope`), or an identifier it
    validated itself.
  - Such a value must also be unchanged by the canonical sanitizer and appear
    in the detail as sent (`disclosed`, applied by `DisplayError` and every
    structured-refusal builder).
  - Caller- or service-controlled text -- a rejected scope, link, mode,
    category or content type, an upstream's own words -- is never a
    parameter, however spelled or encoded. Such a refusal fails closed:
    `unclassified` with no params, and the interface shows the English detail,
    which this rule never alters.
- **The presenter.** `api()` (app.js) is the one presentation owner. It says
  `error.<code>` (a coded cause too), else `error.category.<category>`.
  - An absent, unknown or `unclassified` code, or a coded cause the catalog
    cannot say, keeps the server's English `detail`.
  - The `Error` keeps `detail`, `status`, `code`, `params`, `english` and
    `localized`, and no request is repeated.
  - The search box and Delivery Profiles prefer the localized words
    (`error.localized`).
- **Field names.** A validator sentence that names its field does so by a
  stable identifier, as a coded parameter (`error.aria2.field.<option>`),
  never by its English label.
- **Proof.** `backend/tests/test_error_codes.py` and
  `frontend/browser/localization-error-codes.spec.js`.

**Limits (WS3e), recorded for direction:**

- **Persisted English.** An archive-extraction refusal is stored as the
  transfer's `extraction_error` text (shown in Details, the extraction toast
  and Discord). A code needs an additive stored field: a schema, backup and
  restore change.
- **Activity Log journal text.** It is stored English, keyed by event type.
- **Plain-text bodies.** The `Forbidden` (unhandled `PermissionError`),
  maintenance, sealed-storage and restore-unresolved handlers answer
  `text/plain`. Turning them into JSON would change their shape, so the
  interface shows their English.
- **Unclassified text.** A provider's or service's own words, and a connection
  test's failure text, are `unclassified`.

The interface says all of these exactly as the server sends them.

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
  `integration.group.<id>`;
- a Details candidate's relationship, disposition and source kind are
  `details.candidate.*` by `relationship_code`, `disposition_code` and
  `source_kind` (three additive read-model fields; the English fields are
  unchanged);
- a provider's state and reason in the provider picker are
  `root_provider.status.<status>` and `root_provider.reason.<reason>`, and a
  link's switch outcome is `root_provider.participation.*` /
  `root_provider.refusal.*`;
- the folder picker's reasons and errors are `directory_picker.reason.*` and
  `directory_picker.error.*` by the server's code.

Provider, integration, palette and plan names are proper names and stay as the
server sends them. Activity Log event messages are the server's journal text
and stay English until WS3e gives them codes. So do a server refusal's own
words where the interface shows them (a switch failure's `message`, a
submission claimant's scope label, a route failure's detail).

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
- **A repaint for the words alone is no news.** Details repaints with
  `relabel: true` on `debridpulse:detail-rendered`; the Files-header owners
  (provider, file selection, source switching) then draw again from their
  last answer and an open picker stays open. Owners that re-read Details when
  a list renders ignore a list repaint caused by the language change (a
  window-capture `locale-changed` listener marks it before any page owner
  repaints).
- **Pending controls and dialogs** take catalog keys: `setButtonPending(button,
  true, 'key')` and `DPSettingsModal.open/confirm/prompt({keys: {title,
  titleValues, message, messageValues, accept, acceptValues, cancel,
  cancelValues}})` (a prompt also takes `label`, `hint`, `placeholder`; its
  default Save / Cancel are keyed too), so a pending label or an open dialog is
  re-said in place.
- **Settings keys its markup once** (`ui-settings-page.js`). A field helper
  (`input`, `selectField`, `toggle`, `tuningToggle`, `inlineField`, `card`,
  `groupCard`, `fitLabel`, …) takes words as a `say(key, values)` value, which
  prints as its words and emits `data-i18n` / `data-i18n-attr` /
  `data-i18n-values`; `render()` then re-says them in place, so values, focus,
  open disclosures and a Test in flight are untouched. An element an owner
  writes again later moves its key with its words (`sayInto`, `sayAttr`).
  Plain strings stay plain. A busy label given as a key (`setBusy`) is re-said
  while it shows.
- **Arguments.** `data-i18n-values` gives an element's attribute keys their
  arguments too, and an argument may itself be a message, `{"$t": key, "$v":
  arguments}` (a card title inside "Help: {subject}"), said in the shown
  language.
- **Names Intl writes.** The subtitle language list keeps its English names in
  English; any other language names each code with `Intl.DisplayNames` and
  sorts by that language. A language change re-names the select's options in
  place (value and selection kept).
- **An open dropdown follows its select.** A searchable select's search name
  (`data-dp-dropdown-search`) is keyed like any attribute. The shared dropdown
  (`ui-accessibility-runtime.js`), the one painter of the projected list,
  redraws an open list whenever its select changes -- including once after a
  language change, when every owner has said its words -- keeping the typed
  query, the search field, the focused option, the list's scroll and the
  selection, and re-reading the search field's name.
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

## 3. Census (WS0) — snapshot at WS4b

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

**Correction made in WS3e.** The WS0 server census counted only
keyword `detail=` and `raise X("…")`. It missed positional
`HTTPException(400, "…")` details and the words of structured refusals under
`api/` and `main.py`, which it now counts (each with its exception type and
whether its raise names a code). Corrected, the server baseline is **768**
entries, not 669.

**Correction made in WS3a.** The WS0 census read any plain sentence containing
a full stop as a CSS selector and dropped it. It also missed the compact-wording
attributes (`data-short`, `data-shortest`, `data-placeholder-concise`,
`data-placeholder-minimum`). Corrected, the WS0 baseline is **3,719** frontend
entries (3,038 unique English), not the 3,350 the WS0 packet reported.

| Measure | At WS3a | At WS3b | At WS3c | At WS3d | At WS4a | At WS4b |
|---|---|---|---|---|---|---|
| Frontend literal entries still outside the catalog | 3,118 (2,581 unique English) | 2,471 (2,093 unique English) | 1,715 (1,478 unique English) | 973 (889 unique English) | 405 (361 unique English) | 0 |
| … rich text / with placeholders | 444 / 234 | 408 / 176 | 371 / 89 | 341 / 25 | 0 / 0 | 0 / 0 |
| … critical-warning (destructive, security, data loss) | 231 | 211 | 176 | 98 | 34 | 0 |
| … long-form Help | 962 | 962 | 962 | 962 | 405 | 0 |
| Exempt, with reasons (`nonlocalizable.json`) | 29 | 49 | 322 | 349 | 353 | 353 |
| Server display messages (`detail`) | 669 | 669 | 669 | 669; at WS3e corrected to 768: 278 coded, 490 exempt, 0 outstanding | 768: 278 coded, 490 exempt, 0 outstanding | unchanged |
| Catalog keys (English) | 690 | 1,214 | 1,599 | 2,197; at WS3e 2,592 (+308 refusal codes, +82 transfer categories, +2 OIDC window) | 3,129 (+537) | 3,467 (+338) |

**WS4 in all.** The 973 Help entries at WS3d (98 critical-warning) are WS4a's
568 (64 critical) and WS4b's 405 (34 critical): 972 keyed and 1 exempt (an
internal section id), each listed with its key in the two candidates'
census reconciliations.

**WS4a notes.** The Field Manual's 568 entries (557 in `ui-help-page.js`, 11
in the License viewer) are 567 keyed and 1 exempt (the internal section id
`quickstart`); the census also finds 3 new literals, the argument values
`Authorization: Bearer <token>` (twice) and the copyright notice, exempt with
their reasons (3 register entries, 4 occurrences in all). 537 new keys: 513 passages and
titles (`help.<section>.<topic>.<part>`), the section labels (`help.tab.*`),
the header's identity levels (`help.header.*`) and its accessible name, the
License viewer's words (`help.legal.*`) and the navigator's tooltip
(`nav.section.current`). Rules:

- The reader's context decides which passages exist; each wording of a
  passage that differs by reader is its own message, named for the condition
  that chooses it (`….standalone`, `….easy`, `….admin`,
  `….activity_log_shown`), never a sentence joined from parts.
- A `<code>` span is an argument (`{link}`, `{path}`, `{file_type}`,
  `{literal}`): commands, link forms, paths and values are shown exactly in
  every language. An argument may not share its name with a tag its message
  uses (the validator refuses it: the formatter is given both together).
- Bold words in a passage quote the interface's own labels; their context
  asks the translator for the words the interface shows. They are not
  separate keys: a passage is translated whole.
- The composed manual names its keys (`data-i18n`, `data-i18n-rich`) and
  `DPI18n.render()` says them, so a language change re-says it in place;
  Help listens only to refit its navigator.
- The License viewer's own words are keyed; the document's text and its own
  title are the bundled English, shown exactly.

**WS4b notes.** Settings Help's 405 entries (34 critical-warning) are all
keyed: 375 in 336 new keys (`help.topic.<topic>.<section>.<part>`; headings
used by several topics are `help.topic.heading.*`, and words several topics
share are `help.topic.shared.*`), 30 by Settings' own keys with the same words
and meaning -- each topic's title is its card's title (`settings.*.title`,
`settings.tuning.title` with the card's own method label), and the role
names are `settings.role.*` -- and the role table's own words
(`help.roles.*`, with its two answers `help.roles.yes` / `help.roles.no`).
Rules:

- A topic part is a catalog key, or said words with their arguments
  (`say(key, values)`); a part under a condition (`when`) is a whole message
  per wording, named for the condition where two compete
  (`….provider_additional_settings_shown` / `_hidden`, `….server` /
  `….standalone`).
- Technical text is an argument, shown exactly: `{variable}`
  (`PUBLIC_BASE_URL`), `{format}` (`issuer|subject-id`), `{claim}`
  (`email_verified=true`), `{scope}` (`openid`), `{file_type}` (`.torrent`),
  and the size values a tuning field accepts (`{lowest}`, `{highest}`,
  `{default}`).
- The role table's answers are facts: `data-answer` is `yes` or `no` from the
  answer itself, whatever its words.
- Every node keeps its key, so an open topic -- its title through the dialog
  shell's said words -- is re-said in place on a language change.
- Help sentences that extend a Settings or server message keep their own
  keys rather than joining the message to more words: the live-tuning
  paragraph (beyond `settings.live_tuning.confirm_unknown`), and the API
  token sentences (beyond `error.account.tokens_integration_only`). The
  contract tests still check that Help names what Settings and the server say.

No WS3a–WS3d or WS4 owner holds an entry outside the catalog or the exemption
register, `index.html` holds none, and `ui-settings-page.js` is whole
(`test_localization_catalogs.py`). The 272 WS3c exemptions are mostly the 223
subtitle language names (above) and provider and protocol names. WS3d's 742
entries (683 in `ui-settings-page.js`, 44 in the Backups manager, 11 in
persistence, 4 in the OIDC verification window) are 709 keyed and 33 exempt
(21 new register entries; 27 occurrences remain in the source today):
request field names, stored default values (the OIDC provider name, the
Discord sender name), example placeholders, OIDC scope identifiers, *arr and
download-client names, a legacy mode summary nothing displays, and two
internal markers.

**WS3d notes.** The OIDC verification window loads no catalog: it is said in
the opener's language through the opener's runtime, and it names the key its
outcome is said from. The OIDC test popup is another document written once, in
the shown language. Backups manager dates follow the shown language
(`DPI18n.intlLocale()`); an open manager redraws its inventory on a language
change, keeping its selection and focus.

**Help dependencies (WS4).** Settings Help repeats some of these words, and
its contract tests tie them together: the live-tuning sentence
(`ui-settings-help.js`, "Changes also apply to existing downloads…"), the
Media Downloads control labels it names, and the Settings locations it
points at. WS4 must say them from the same keys (or keep the tests' check
that Help names what Settings shows). WS3d adds (checked against
`ui-settings-help.js`): Help names the role names (`settings.role.*`, e.g.
"ReadOnly User") and these cards' titles and control labels -- Deployment
Type, Integration Delivery Profiles, Merge, Backups & Retention, Backup Folder,
Run Backup, Allow Database Reset, Activity Log Page Size, Primary Webhook, OIDC
Callback URL, Default Role for New OIDC Users (`settings.deployment.*`, `settings.backups.*`,
`settings.reset.*`, `settings.events.*`, `settings.webhook.*`,
`settings.oidc.*`). WS4 says them from the same keys. WS3e adds (checked against the Help sources): Help repeats two
refusals' words -- "Only Integration accounts hold API tokens"
(`error.account.tokens_integration_only`) and "… is not a supported link"
(`error.submit.link_unsupported`) -- which WS4 says from the same keys.

*As done in WS4:* topic titles and the role names are said from these same
keys. A label or a message named inside a Help sentence stays part of that
sentence (a passage is translated whole); the contract tests keep checking
that Help names what Settings and the server say, and WS5 checks the
translations' terms against the interface's.

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
- an argument named like a rich-text tag its message uses (`<code>{code}</code>`);
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

### WS5 translation checks

For each target translation the validator also renders the message at sample
values (counts 0 1 2 3 5 11 21 101 1.5, every select case) and hard-fails:

- rich-text tags that differ from the English rendering (dropped, added, moved);
- select cases that differ from English; a plural lacking a CLDR category the
  locale reaches with whole numbers (`pl`/`ru`/`uk` few and many; `ar` zero, two,
  few and many; `es`/`fr`/`it`/`pt` many), or an exact `=N` case English has, or an
  English plural that inflects said plainly in a language with several categories;
- a rendering identical to English, unless the review ledger records it as
  intentionally identical (`identical`);
- a protected name missing, or a protected literal (`https://`,
  `PUBLIC_BASE_URL`, `MB/s`…) changed in number (`terms/en.json` `protected`);
- terminology: a concept key not translated as the locale's term, a forbidden
  alternative, or English naming a concept (its `match`) where the translation
  uses neither the term nor a listed form. An exception is recorded per key in
  the ledger (`terms`);
- Help control labels: where `en.labels.json` marks a span `control` (it names an
  interface element by its displayed name), one of the translation's `<b>` spans
  must say that element's current translation (a lead-in colon and an `A → B`
  path are allowed). A `term` span (a concept in prose) may inflect; `emphasis`,
  `control-unmapped` and `unresolved` spans are not checked. An exception is
  recorded in the ledger (`labels`);
- a malformed `sources` hash, a hash without its translation, a catalog without
  its schema, and a review ledger that is malformed or claims a human
  translation or review without naming its reviewer and evidence.

Translations that still contain common English words are listed as a warning.

**Review debt** (a warning): a translation without a current review (a ledger entry
for the current English and the current text, with `ai-review` or `human-review`),
a critical key without a current back-translation, and open findings (critical
ones listed apart). **`--complete=TAG`** is the candidate check for one planned locale:
zero translation debt, a termbase term for every concept, no review debt. `--release`
applies the same to every `supported` locale.

Nothing here certifies a translation. Automated checks prove structure, literals
and recorded terminology. Meaning, register and the full consequence of a critical
warning need review, and the ledger's provenance (`ai-generated`, `ai-review`,
`ai-back-translation`, or `human-*` with reviewer and evidence) says which review
happened.

**A termbase** is `{"schema": "debridpulse-terms/1", "locale", "register", "terms":
{concept: {"target", "forms"?, "forbidden"?}}}`. **A ledger** is `{"schema":
"debridpulse-review/1", "locale", "entries": {key: {"source", "translation",
"provenance", "backTranslation"?, "human"?: {"reviewer", "evidence"}, "findings"?:
[{"kind", "note", "status", "resolution"?}], "identical"?, "terms"?, "labels"?}}}`,
where `source` and `translation` are the sha256[:16] of the English and the translation reviewed.

**The review sheet** (`node .github/localization/review_sheet.mjs --locale=TAG
--out=FILE.html`) writes every key with its context, English, translation (plurals
and selects at their samples), the Help control labels it must show, and the
ledger's record, as HTML and CSV.

**The locale probe** (`DP_LOCALE=<tag> npx playwright test localization-locale.spec.js`)
shows the locale on an instance's own reader and fails on catalog or semantic
defects: English left where the locale translates it, keys shown, debt, runtime
notes, critical text in English, Help control labels, or a change in what exists
(navigation, Settings tabs, Help passages, every topic's parts and the role answers
for twelve reader contexts). It records geometry at 320 px for WS6 without failing,
and marks an unloadable catalog `INFRASTRUCTURE`. With `DP_PROBE_OUT` it writes its
findings as JSON. It sees only what that reader is shown.

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
