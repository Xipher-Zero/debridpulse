# Localization

DebridPulse 1.0.14 status: **foundation complete, product localization incomplete.**
The runtime, the catalogs, the validators, the pseudo locales, the per-account
language and the coded-error seam exist. Only the proof surfaces read the catalog:
the shell's navigation and page headings, My Account and its Help, the sign-in
pages, and the account error codes. Every other surface is still English literals
(the WS3/WS4 debt the census below counts). No language is offered: there is no
picker, and no target catalog ships.

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
`preauth_locale(explicit)` is the hook a future explicit pre-sign-in choice
would use.

**Errors.** A `DisplayError` keeps its English `detail` exactly as API clients
already receive it, and adds `code` and `params`. The interface shows
`error.<code>` from the catalog and falls back to `detail`.

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

## 3. Census (WS0) — snapshot at this candidate

Run `python .github/localization/inventory.py --summary` to get the counts, or
add `--json OUT` for the full register. Each entry carries an ID, the English
text, owner and line, context, placeholders, and criticality. The census reads
source, so conditionally rendered variants are all counted.

| Measure | Count |
|---|---|
| Frontend literal entries still outside the catalog | 3,350 (2,683 unique English) |
| … rich text / with placeholders | 506 / 266 |
| … critical-warning (destructive, security, data loss) | 201 |
| … long-form Help | 806 |
| Server display messages (`detail`) | 668 |
| Catalog keys (English) | 165 |

The largest owners still outside the catalog:

| Owner | Entries |
|---|---|
| `ui-settings-page.js` | 1,226 |
| `ui-help-page.js` | 538 |
| `ui-settings-help.js` (all topics except My Account) | 268 |
| `app.js` | 239 |
| `index.html` | 235 |
| `ui-downloads.js` | 131 |

Every remaining entry belongs to WS3 (application screens) or WS4 (Help).

The census still has known noise:

- `ui-i18n.js` diagnostics, which are console-only;
- the internal `Error('Logout failed')` in `auth.js`, which is never shown;
- the theme picker's palette names;
- a language-name list that should come from `Intl.DisplayNames` and not from the catalog.

**Expansion-risk map.** Under `en-XA` these surfaces need the most care:

- the handheld dock's five 64 px slots, which currently ellipsize;
- every Settings master header, which must stay one row from 320 px;
- the My Account header at 320 px. Its title already wraps to two lines under `en-XA` at 390 px, a WS3 finding;
- table headers and status chips;
- the sidebar's collapsed rail tooltips.

## 4. Translation debt and catch-up

Run `node .github/localization/validate.mjs` (add `--json` for the full report).

**Hard errors** fail with exit 1:

- a non-canonical BCP 47 tag;
- English messages and metadata out of step;
- a non-semantic key;
- metadata missing a category, context, existing owner or criticality;
- an unparsable message;
- positional `{0}` arguments;
- arguments that differ from those declared or from the English source, by name or type;
- tags outside the allowed set;
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
