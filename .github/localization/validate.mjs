// DebridPulse catalog validator (no network, no installed packages).
//
//   node .github/localization/validate.mjs [--json] [--release] [--i18n=DIR] [--meta=FILE]
//
// Reads frontend/static/i18n/manifest.json, every catalog there and the
// English source metadata (frontend/i18n/en.meta.json), and parses each
// message with the same formatter the interface uses (the vendored FormatJS
// intl-messageformat), so what passes here formats there.
//
// HARD ERRORS (exit 1): a locale tag that is not canonical BCP 47; English
// messages and metadata out of step; a key that is not a semantic dotted key;
// metadata without category, context, owner or criticality; a message that
// does not parse; positional ({0}) arguments; arguments that differ from the
// English source (names or types) or from the declared ones; tags outside
// b/strong/em/i/code/kbd, or an argument named like a tag its message uses;
// literal text that reads as markup ('<' then a
// letter, '/', '!' or '?': a translation is text, so whatever an owner does
// with a formatted message, it never carries an element); plural categories
// the locale does not have
// (Intl.PluralRules, CLDR) or a plural without `other`; a sign-in page
// message (login.*) using more than plain {name} arguments; a translated key
// English does not have.
//
// A translation may give an argument the English source uses plainly
// ({count}) a plural or number form where the English metadata declares that
// argument a number ("number: …" / "plural: …"), so a language can inflect a
// count English does not.
//
// WARNINGS (translation debt, exit 0): for each target locale, the English
// keys it has no translation of (missing) and the ones translated from an
// older English text (stale: its `sources` hash differs), with the
// critical-warning keys among the missing and among the stale each listed
// apart. A key in debt is shown in English, key by key: the server delivers
// a target's current translations only (backend/core/localization.py
// `delivered`, the same hash), so a stale one never reaches the interface.
//
// --release: every locale the manifest marks `supported` must have zero
// debt; any debt is then an error.
import {readFileSync, existsSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const option = name => process.argv.slice(2).find(arg => arg.startsWith(`--${name}=`))?.split('=').slice(1).join('=');
// The catalogs and metadata to check (the repository's unless given).
const I18N = option('i18n') || join(ROOT, 'frontend', 'static', 'i18n');
const META = option('meta') || join(ROOT, 'frontend', 'i18n', 'en.meta.json');
const VENDOR = join(ROOT, 'frontend', 'static', 'vendor', 'intl-messageformat.esm.js');
const RICH_TAGS = new Set(['b', 'strong', 'em', 'i', 'code', 'kbd']);
const CATEGORIES = new Set(['critical-warning', 'action', 'card-title', 'page-title', 'modal-title', 'navigation',
  'table-header', 'status-chip', 'placeholder', 'tooltip/aria', 'long-form-help', 'label', 'hint', 'toast', 'error']);
const KEY = /^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$/;
const TYPE = {LITERAL: 0, ARGUMENT: 1, NUMBER: 2, DATE: 3, TIME: 4, SELECT: 5, PLURAL: 6, POUND: 7, TAG: 8};
const TYPE_NAME = {1: 'string', 2: 'number', 3: 'date', 4: 'time', 5: 'select', 6: 'plural'};

const args = new Set(process.argv.slice(2).filter(arg => !arg.includes('=')));
const source = readFileSync(VENDOR, 'utf8');
const {IntlMessageFormat} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

const errors = [];
const error = (where, what) => errors.push(`${where}: ${what}`);
const read = path => JSON.parse(readFileSync(path, 'utf8'));
const sha = text => createHash('sha256').update(text).digest('hex').slice(0, 16);

const manifest = read(join(I18N, 'manifest.json'));
for (const tag of Object.keys(manifest.locales)) {
  let canonical = null;
  try { canonical = Intl.getCanonicalLocales(tag)[0]; } catch (_) {}
  if (canonical !== tag) error(`manifest ${tag}`, 'not a canonical BCP 47 tag');
  if (!['canonical', 'planned', 'supported', 'pseudo'].includes(manifest.locales[tag].status)) {
    error(`manifest ${tag}`, 'unknown status');
  }
  if (!['ltr', 'rtl'].includes(manifest.locales[tag].dir)) error(`manifest ${tag}`, 'dir is not ltr or rtl');
}

// Arguments and tags of a parsed message: name -> type, and every tag name.
function shape(elements, out = {args: {}, tags: new Set(), plurals: [], positional: false, markup: false}) {
  for (const element of elements) {
    if (element.type === TYPE.LITERAL && /<[A-Za-z!\/?]/.test(element.value)) out.markup = true;
    if (element.type in TYPE_NAME) {
      if (/^\d+$/.test(element.value)) out.positional = true;
      const type = TYPE_NAME[element.type];
      if (out.args[element.value] && out.args[element.value] !== type) out.args[element.value] += `|${type}`;
      else out.args[element.value] = type;
    }
    if (element.type === TYPE.TAG) {
      out.tags.add(element.value);
      shape(element.children, out);
    }
    if (element.type === TYPE.SELECT || element.type === TYPE.PLURAL) {
      if (element.type === TYPE.PLURAL) out.plurals.push({name: element.value, options: Object.keys(element.options),
        ordinal: element.pluralType === 'ordinal'});
      for (const option of Object.values(element.options)) shape(option.value, out);
    }
  }
  return out;
}

function parse(where, message, tag) {
  if (typeof message !== 'string') { error(where, 'message is not a string'); return null; }
  try {
    return shape(new IntlMessageFormat(message, tag.endsWith('-XA') || tag.endsWith('-XB') ? 'en' : tag).getAst());
  } catch (e) {
    error(where, `does not parse: ${e.message}`);
    return null;
  }
}

function checkPlurals(where, found, tag) {
  for (const plural of found.plurals) {
    const categories = new Intl.PluralRules(tag, {type: plural.ordinal ? 'ordinal' : 'cardinal'}).resolvedOptions().pluralCategories;
    if (!plural.options.includes('other')) error(where, `plural {${plural.name}} has no 'other'`);
    for (const option of plural.options) {
      if (!option.startsWith('=') && !categories.includes(option)) error(where, `plural category '${option}' is not one of ${tag}'s (${categories.join(', ')})`);
    }
  }
}

const english = read(join(I18N, 'en.json'));
const meta = read(META).meta;
const englishShape = {};
for (const key of new Set([...Object.keys(english.messages), ...Object.keys(meta)])) {
  const where = `en ${key}`;
  if (!(key in english.messages)) { error(where, 'metadata without an English message'); continue; }
  if (!(key in meta)) { error(where, 'English message without metadata'); continue; }
  if (!KEY.test(key)) error(where, 'not a semantic dotted key');
  const m = meta[key];
  if (!CATEGORIES.has(m.category)) error(where, `unknown category '${m.category}'`);
  if (!m.context || m.context.length < 8) error(where, 'no translator context');
  if (!['normal', 'critical-warning'].includes(m.criticality)) error(where, 'criticality is not normal or critical-warning');
  if (!m.owner || !existsSync(join(ROOT, m.owner))) error(where, `owner '${m.owner}' does not exist`);
  const found = parse(where, english.messages[key], 'en');
  if (!found) continue;
  englishShape[key] = found;
  if (found.positional) error(where, 'positional arguments ({0}); name every argument');
  if (found.markup) error(where, 'literal text reads as markup');
  for (const tag of found.tags) if (!RICH_TAGS.has(tag)) error(where, `tag <${tag}> is not allowed`);
  // An argument named like a rich-text tag the message uses would replace that
  // tag's element when it is said (ui-i18n.js passes tags and arguments together).
  for (const name of Object.keys(found.args)) if (found.tags.has(name)) error(where, `argument {${name}} is named like the message's rich-text tag <${name}>`);
  const declared = Object.keys(m.args || {}).sort().join(',');
  if (declared !== Object.keys(found.args).sort().join(',')) error(where, `arguments (${Object.keys(found.args)}) differ from the declared (${declared})`);
  checkPlurals(where, found, 'en');
  if (key.startsWith('login.') && (found.tags.size || Object.values(found.args).some(type => type !== 'string'))) {
    error(where, 'a sign-in page message may use plain {name} arguments only');
  }
}

const report = {locales: {}, keys: Object.keys(english.messages).length};
for (const [tag, info] of Object.entries(manifest.locales)) {
  if (tag === manifest.default || info.status === 'pseudo') continue;
  const path = join(I18N, `${tag}.json`);
  const catalog = existsSync(path) ? read(path) : {messages: {}, sources: {}};
  if (existsSync(path) && catalog.locale !== tag) error(`${tag}`, `catalog says locale '${catalog.locale}'`);
  const missing = [], stale = [];
  for (const [key, message] of Object.entries(catalog.messages)) {
    const where = `${tag} ${key}`;
    if (!(key in english.messages)) { error(where, 'not an English key'); continue; }
    const found = parse(where, message, tag);
    if (!found || !englishShape[key]) continue;
    const expected = englishShape[key].args;
    if (found.markup) error(where, 'literal text reads as markup');
    // An argument may appear several times (a plural that also shows its count):
    // its uses are compared as sets. A count -- an argument English selects a
    // plural on or formats as a number, or declares a number -- may be shown
    // plainly, pluralized or formatted in any translation.
    const counted = name => /^(number|plural)\b/.test(String(meta[key]?.args?.[name] || ''));
    for (const [name, type] of Object.entries(found.args)) {
      if (!(name in expected)) { error(where, `argument {${name}} is not in the English source`); continue; }
      const allowed = new Set(expected[name].split('|'));
      if ([...allowed].some(each => each === 'plural' || each === 'number') || counted(name)) ['plural', 'number', 'string'].forEach(each => allowed.add(each));
      const refused = type.split('|').filter(each => !allowed.has(each));
      if (refused.length) error(where, `argument {${name}} is a ${type}, the English source's a ${expected[name]}`);
    }
    for (const name of Object.keys(expected)) if (!(name in found.args)) error(where, `argument {${name}} of the English source is missing`);
    for (const t of found.tags) if (!RICH_TAGS.has(t) || !englishShape[key].tags.has(t)) error(where, `tag <${t}> is not the English source's`);
    checkPlurals(where, found, tag);
    if ((catalog.sources || {})[key] !== sha(english.messages[key])) stale.push(key);
  }
  for (const key of Object.keys(english.messages)) if (!(key in catalog.messages)) missing.push(key);
  const isCritical = key => meta[key]?.criticality === 'critical-warning';
  const criticalMissing = missing.filter(isCritical), criticalStale = stale.filter(isCritical);
  report.locales[tag] = {status: info.status, catalog: existsSync(path), missing: missing.length, stale: stale.length,
    critical: criticalMissing.length + criticalStale.length, criticalMissing: criticalMissing.length,
    criticalStale: criticalStale.length, missingKeys: missing, staleKeys: stale,
    criticalKeys: [...criticalMissing, ...criticalStale], criticalMissingKeys: criticalMissing,
    criticalStaleKeys: criticalStale};
  if (args.has('--release') && info.status === 'supported' && (missing.length || stale.length)) {
    error(tag, `advertised as supported with ${missing.length} missing and ${stale.length} stale keys`);
  }
}

report.errors = errors;
if (args.has('--json')) {
  process.stdout.write(JSON.stringify(report, null, 1) + '\n');
} else {
  for (const line of errors) console.log(`ERROR ${line}`);
  for (const [tag, row] of Object.entries(report.locales)) {
    if (row.missing || row.stale) {
      console.log(`WARNING translation debt ${tag} (${row.status}): ${row.missing} missing, ${row.stale} stale; `
        + `critical-warning: ${row.criticalMissing} missing, ${row.criticalStale} stale`);
    }
  }
  console.log(`${report.keys} English keys; ${errors.length} errors`);
}
// exitCode, not exit(): a large report written to a pipe is flushed first.
process.exitCode = errors.length ? 1 : 0;
