// DebridPulse catalog validator (no network, no installed packages).
//
//   node .github/localization/validate.mjs [--json] [--release] [--complete=TAG] [--i18n=DIR] [--meta=FILE]
//                                         [--source=DIR] [--labels=FILE] [--terms=FILE]
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
//
// WS5 translation checks (docs/LOCALIZATION.md §4). For each target
// translation, HARD ERRORS also: a message rendered at sample values
// (counts 0 1 2 3 5 11 21 101 and 1.5, each select case) whose rich-text
// tags differ from the English rendering (dropped, added or moved between
// branches); a select whose cases differ from English; a plural lacking a
// CLDR category the locale reaches with whole numbers (pl/ru/uk few+many,
// ar zero/one/two/few/many, es/fr/it/pt many), or an exact `=N` case English
// has, or an English plural said plainly where the locale has several
// categories; a rendering identical to English (untranslated) unless the
// review ledger records it as intentionally identical; a protected name
// absent, or a protected literal (URL syntax, identifier, unit) changed in
// number, from the English (frontend/i18n/terms/en.json `protected`); the
// locale's termbase term for a concept not used where English names it, the
// concept key not translated as its term, or a forbidden alternative used
// (frontend/i18n/terms/<tag>.json; an exception is recorded per key in the
// ledger); a Help control reference (frontend/i18n/en.labels.json, kind
// `control`) whose element's current translation is not one of the Help
// translation's <b> spans; a malformed `sources` hash, a hash without its
// translation, or a catalog without its schema; a review ledger
// (frontend/i18n/review/<tag>.json) that is malformed or claims a human
// review without naming its reviewer and evidence.
//
// REVIEW DEBT (warnings): translations without a review ledger entry for
// their current English and text, without an independent review, critical
// keys without a back-translation, and open findings (critical ones apart).
// Nothing here certifies a translation: automated checks prove structure,
// literals and recorded terminology, not meaning.
//
// --complete=TAG: the WS5 candidate check for one (planned) locale: zero
// translation debt, a termbase for every concept, every translation reviewed,
// every critical key back-translated, no open finding. --release applies the
// same to every `supported` locale.
import {readFileSync, existsSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {IntlMessageFormat, RICH_TAGS, TYPE, render, samples, plainText, boldSpans, sampleName, isPlaceholder} from './messages.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const option = name => process.argv.slice(2).find(arg => arg.startsWith(`--${name}=`))?.split('=').slice(1).join('=');
// The catalogs and metadata to check (the repository's unless given).
const I18N = option('i18n') || join(ROOT, 'frontend', 'static', 'i18n');
const META = option('meta') || join(ROOT, 'frontend', 'i18n', 'en.meta.json');
// Unserved translation sources: per-locale termbases and review ledgers (beside
// the metadata unless given), the English concept register and the Help label map.
const SOURCE = option('source') || dirname(META);
const CONCEPTS = option('terms') || join(ROOT, 'frontend', 'i18n', 'terms', 'en.json');
const LABELS = option('labels') || join(ROOT, 'frontend', 'i18n', 'en.labels.json');
const COMPLETE = option('complete');
const CATEGORIES = new Set(['critical-warning', 'action', 'card-title', 'page-title', 'modal-title', 'navigation',
  'table-header', 'status-chip', 'placeholder', 'tooltip/aria', 'long-form-help', 'label', 'hint', 'toast', 'error']);
const KEY = /^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$/;
const TYPE_NAME = {1: 'string', 2: 'number', 3: 'date', 4: 'time', 5: 'select', 6: 'plural'};

const args = new Set(process.argv.slice(2).filter(arg => !arg.includes('=')));

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

// ── WS5: the English registers translations are checked against ─────────────
const escapeRe = text => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const LETTER = /[\p{L}\p{N}]/u;
// A name stands alone where it begins or ends with a letter or digit; a literal is found as written.
const named = text => new RegExp(`${LETTER.test(text[0]) ? '(?<![\\p{L}\\p{N}])' : ''}${escapeRe(text)}${LETTER.test(text.at(-1)) ? '(?![\\p{L}\\p{N}])' : ''}`, 'gu');
const occurrences = (text, pattern) => (text.match(pattern) || []).length;

const concepts = existsSync(CONCEPTS) ? read(CONCEPTS) : null;
const PROTECTED = [], CONCEPT = {};
if (!concepts) error('terms', `the English concept register ${CONCEPTS} is missing`);
else {
  if (concepts.schema !== 'debridpulse-terms/1' || concepts.locale !== 'en') error('terms en', 'not a debridpulse-terms/1 English register');
  for (const [index, entry] of (concepts.protected || []).entries()) {
    if (typeof entry?.text !== 'string' || !entry.text || !['name', 'literal'].includes(entry.kind) || !entry.reason) {
      error(`terms en protected[${index}]`, 'needs text, kind (name or literal) and reason');
    } else PROTECTED.push({...entry, pattern: named(entry.text)});
  }
  for (const [id, entry] of Object.entries(concepts.concepts || {})) {
    const where = `terms en ${id}`;
    if (!/^[a-z][a-z0-9_]*$/.test(id)) error(where, 'not a concept id');
    if (!entry || typeof entry.en !== 'string' || !entry.note) { error(where, 'needs en and note'); continue; }
    if (entry.key && !(entry.key in english.messages)) error(where, `key '${entry.key}' is not an English key`);
    if (entry.key && english.messages[entry.key] !== entry.en) error(where, `key '${entry.key}' does not say '${entry.en}'`);
    let match = null;
    if (entry.match) try { match = new RegExp(entry.match, entry.flags ?? 'iu'); } catch (e) { error(where, `match does not compile: ${e.message}`); }
    CONCEPT[id] = {...entry, match};
  }
}

// The Help label map: every <b> span of a help.* message, in order, classified.
const BOLD = /<b>([^<]*)<\/b>/g;
const KINDS = new Set(['control', 'term', 'emphasis', 'control-unmapped', 'unresolved']);
const labelMap = existsSync(LABELS) ? read(LABELS) : null;
const labels = {spans: 0, control: 0, term: 0, emphasis: 0, 'control-unmapped': 0, unresolved: 0};
const controlsOf = {};                       // help key -> [{index, keys, join, suffix}]
if (!labelMap || labelMap.schema !== 'debridpulse-help-labels/1') error('labels', `the Help label map ${LABELS} is missing or not debridpulse-help-labels/1`);
else {
  const spans = labelMap.spans || {};
  for (const key of new Set([...Object.keys(spans), ...Object.keys(english.messages).filter(each => each.startsWith('help.'))])) {
    const found = key in english.messages ? [...english.messages[key].matchAll(BOLD)].map(m => m[1]) : null;
    const listed = spans[key] || [];
    const where = `labels ${key}`;
    if (!found) { error(where, 'not an English key'); continue; }
    if (found.length !== listed.length || found.some((text, i) => listed[i]?.text !== text)) {
      error(where, `lists ${listed.length} spans, the English has ${found.length} (${found.join(' | ')}): map each <b> span in order`);
      continue;
    }
    listed.forEach((entry, index) => {
      const at = `${where}#${index}`;
      labels.spans += 1;
      if (!KINDS.has(entry.kind)) { error(at, `unknown kind '${entry.kind}'`); return; }
      labels[entry.kind] += 1;
      if (!entry.reason) error(at, 'no reason');
      const keys = entry.keys || (entry.key ? [entry.key] : []);
      for (const each of keys) if (!(each in english.messages) || each.startsWith('help.')) error(at, `'${each}' is not an interface key`);
      if (entry.kind !== 'control') return;
      if (!keys.length) { error(at, 'a control names its key (key or keys)'); return; }
      const said = keys.map(each => english.messages[each]).join(` ${entry.join || ''} `.replace(/ {2}/, ' ')) + (entry.suffix || '');
      if (keys.length > 1 && !entry.join) error(at, 'a composed control names its join');
      if (said.trim() !== entry.text.trim()) error(at, `its keys say '${said}', the span '${entry.text}'`);
      (controlsOf[key] = controlsOf[key] || []).push({index, keys, suffix: entry.suffix || ''});
    });
  }
}

// ── WS5: rendered checks (samples and rendering: messages.mjs) ───────────────
const tagCounts = text => {
  const counts = {};
  for (const m of text.matchAll(/\uE000(\/?)([a-z]+)\uE001/g)) counts[`${m[1]}${m[2]}`] = (counts[`${m[1]}${m[2]}`] || 0) + 1;
  return JSON.stringify(Object.keys(counts).sort().map(name => [name, counts[name]]));
};

// The plural categories a locale reaches with whole numbers (CLDR, Intl.PluralRules).
const reached = new Map();
function wholeCategories(tag) {
  if (!reached.has(tag)) {
    const rules = new Intl.PluralRules(tag);
    const found = new Set();
    for (let n = 0; n <= 1000; n += 1) found.add(rules.select(n));
    for (const n of [1e4, 1e5, 1e6, 2e6]) found.add(rules.select(n));
    reached.set(tag, ['zero', 'one', 'two', 'few', 'many', 'other'].filter(category => found.has(category)));
  }
  return reached.get(tag);
}
// Each plural/select of a message by argument: {kind, options:Set}.
function branches(message, tag) {
  const out = {};
  const walk = elements => elements.forEach(element => {
    if (element.type === TYPE.SELECT || element.type === TYPE.PLURAL) {
      const kind = element.type === TYPE.SELECT ? 'select' : 'plural';
      const entry = out[element.value] = out[element.value] || {kind, options: new Set(), all: []};
      Object.keys(element.options).forEach(o => entry.options.add(o));
      entry.all.push(Object.keys(element.options));
      Object.values(element.options).forEach(o => walk(o.value));
    }
    if (element.type === TYPE.TAG) walk(element.children);
  });
  walk(new IntlMessageFormat(message, tag.endsWith('-XA') || tag.endsWith('-XB') ? 'en' : tag).getAst());
  return out;
}

// English words a translation into a Latin-script language should not still hold (a warning).
const ENGLISH_WORDS = /(?<![\p{L}])(the|your|this|which|should|would|there|their|with)(?![\p{L}])/iu;
const LATIN = new Set(['es', 'fr', 'de', 'it', 'pt-BR', 'nl', 'pt-PT', 'pl', 'tr', 'id']);
const PROVENANCE = new Set(['ai-generated', 'human-translated', 'ai-review', 'human-review', 'ai-back-translation', 'human-back-translation']);
const FINDINGS = new Set(['meaning', 'omission', 'terminology', 'register', 'ambiguity', 'critical', 'style', 'other']);
const isCritical = key => meta[key]?.criticality === 'critical-warning';

const report = {locales: {}, keys: Object.keys(english.messages).length, labels};
for (const [tag, info] of Object.entries(manifest.locales)) {
  if (tag === manifest.default || info.status === 'pseudo') continue;
  const path = join(I18N, `${tag}.json`);
  const catalog = existsSync(path) ? read(path) : {schema: 'debridpulse-catalog/1', messages: {}, sources: {}};
  if (existsSync(path) && catalog.locale !== tag) error(`${tag}`, `catalog says locale '${catalog.locale}'`);
  if (catalog.schema !== 'debridpulse-catalog/1') error(`${tag}`, `catalog schema is '${catalog.schema}', not debridpulse-catalog/1`);
  const sources = catalog.sources && typeof catalog.sources === 'object' ? catalog.sources : {};
  if (catalog.sources !== undefined && sources !== catalog.sources) error(`${tag}`, 'sources is not an object');
  for (const [key, hash] of Object.entries(sources)) {
    if (!(key in catalog.messages)) error(`${tag} ${key}`, 'a source hash without its translation');
    else if (!/^[0-9a-f]{16}$/.test(String(hash))) error(`${tag} ${key}`, `source hash '${hash}' is not sha256[:16] hex`);
  }
  // The locale's termbase and review ledger, where they exist.
  const termsPath = join(SOURCE, 'terms', `${tag}.json`), reviewPath = join(SOURCE, 'review', `${tag}.json`);
  const termbase = existsSync(termsPath) ? read(termsPath) : null;
  const ledger = existsSync(reviewPath) ? read(reviewPath) : null;
  const TERMS = {};
  if (termbase) {
    if (termbase.schema !== 'debridpulse-terms/1' || termbase.locale !== tag) error(`terms ${tag}`, `not a debridpulse-terms/1 termbase for ${tag}`);
    if (!termbase.register) error(`terms ${tag}`, 'no register (the address and formality chosen for this locale)');
    for (const [id, entry] of Object.entries(termbase.terms || {})) {
      if (!(id in CONCEPT)) { error(`terms ${tag} ${id}`, 'not a concept of the English register'); continue; }
      if (typeof entry?.target !== 'string' || !entry.target.trim()) { error(`terms ${tag} ${id}`, 'no target term'); continue; }
      const lower = text => text.toLocaleLowerCase(tag);
      TERMS[id] = {...entry, accepted: [entry.target, ...(entry.forms || [])].map(lower), forbidden: (entry.forbidden || []).map(lower), lower};
    }
  }
  const entries = ledger?.entries || {};
  if (ledger) {
    if (ledger.schema !== 'debridpulse-review/1' || ledger.locale !== tag) error(`review ${tag}`, `not a debridpulse-review/1 ledger for ${tag}`);
    for (const [key, entry] of Object.entries(entries)) {
      const at = `review ${tag} ${key}`;
      if (!(key in english.messages)) error(at, 'not an English key');
      else if (!(key in catalog.messages)) error(at, 'a review of a key the catalog does not translate');
      const provenance = entry?.provenance || [];
      for (const each of provenance) if (!PROVENANCE.has(each)) error(at, `unknown provenance '${each}'`);
      if (provenance.filter(each => each === 'ai-generated' || each === 'human-translated').length !== 1) error(at, 'names exactly one origin (ai-generated or human-translated)');
      if (provenance.some(each => each.startsWith('human-')) && !(entry.human?.reviewer && entry.human?.evidence)) {
        error(at, 'a human translation or review names its reviewer and evidence');
      }
      if (provenance.some(each => each.endsWith('back-translation')) && !entry.backTranslation) error(at, 'a back-translation records its text');
      for (const [index, finding] of (entry.findings || []).entries()) {
        if (!FINDINGS.has(finding?.kind) || !finding.note || !['open', 'resolved'].includes(finding.status)) {
          error(`${at} finding ${index}`, 'needs kind, note and status (open or resolved)');
        } else if (finding.status === 'resolved' && !finding.resolution) error(`${at} finding ${index}`, 'resolved without a resolution');
      }
    }
  }
  const exempt = (key, field, id) => typeof entries[key]?.[field]?.[id] === 'string' && entries[key][field][id].length > 0;
  const missing = [], stale = [], englishFragments = [];
  const unreviewed = [], noBackTranslation = [], openFindings = [], provenance = {};
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
    const current = sources[key] === sha(english.messages[key]);
    if (!current) stale.push(key);

    // Selects and plurals against English, and the categories the locale needs.
    const theirs = branches(message, tag), ours = branches(english.messages[key], 'en');
    for (const name of new Set([...Object.keys(theirs), ...Object.keys(ours)])) {
      const a = ours[name], b = theirs[name];
      if (a?.kind === 'select' || b?.kind === 'select') {
        const left = [...(a?.kind === 'select' ? a.options : [])].sort().join(','), right = [...(b?.kind === 'select' ? b.options : [])].sort().join(',');
        if (left !== right) error(where, `select {${name}} has the cases (${right}), the English (${left})`);
        continue;
      }
      const categories = wholeCategories(tag);
      // English that inflects a count (more than `other`) asks the same of a language with several categories.
      const inflects = a?.kind === 'plural' && [...a.options].some(o => o !== 'other');
      if (inflects && !b && categories.length > 1) error(where, `the English plural {${name}} is said plainly; ${tag} distinguishes ${categories.join(', ')}`);
      if (b?.kind === 'plural' && (inflects || a?.kind !== 'plural')) {
        for (const options of b.all) {
          const lacking = categories.filter(c => !options.includes(c));
          if (lacking.length) error(where, `plural {${name}} lacks ${lacking.join(', ')} (${tag} reaches ${categories.join(', ')} with whole numbers)`);
        }
      }
      if (b?.kind === 'plural') {
        const exact = [...(a?.kind === 'plural' ? a.options : [])].filter(o => o.startsWith('='));
        for (const o of exact) if (!b.options.has(o)) error(where, `plural {${name}} lacks the English exact case ${o}`);
      }
    }

    // Rendered at each sample: tags, untranslated text, protected names and literals, terms.
    let tagError = null, identical = null, literalError = null, fragment = false;
    const termErrors = new Map(), forbidden = new Map(), bold = new Set();
    for (const values of samples(english.messages[key], meta[key]?.args)) {
      let ours, theirs;
      try { ours = render(english.messages[key], 'en', values); theirs = render(message, tag, values); }
      catch (e) { error(where, `does not format at ${sampleName(values)}: ${e.message}`); break; }
      if (!tagError && tagCounts(ours) !== tagCounts(theirs)) tagError = `rich-text tags differ from the English at ${sampleName(values)} (English ${tagCounts(ours)}, translation ${tagCounts(theirs)})`;
      const plainOurs = plainText(ours), plainTheirs = plainText(theirs);
      if (!identical && plainOurs === plainTheirs) {
        let rest = plainOurs.replace(/⟨[^⟩]*⟩|other/g, '');
        for (const entry of PROTECTED) rest = rest.replace(entry.pattern, '');
        if (/\p{L}{2,}/u.test(rest)) identical = sampleName(values) || 'its only form';
      }
      for (const entry of PROTECTED) {
        if (literalError) break;
        const a = occurrences(plainOurs, entry.pattern), b = occurrences(plainTheirs, new RegExp(escapeRe(entry.text), 'gu'));
        if (entry.kind === 'name' && a && !b) literalError = `the protected name '${entry.text}' is missing (it is never translated or transliterated)`;
        if (entry.kind === 'literal' && a !== b) literalError = `the protected literal '${entry.text}' appears ${b} times, the English ${a}`;
      }
      if (LATIN.has(tag) && ENGLISH_WORDS.test(plainTheirs.replace(/⟨[^⟩]*⟩/g, ''))) fragment = true;
      for (const [id, term] of Object.entries(TERMS)) {
        const text = term.lower(plainTheirs);
        for (const word of term.forbidden) if (text.includes(word)) forbidden.set(`${id}:${word}`, word);
        if (CONCEPT[id].match?.test(plainOurs) && !term.accepted.some(form => text.includes(form)) && !exempt(key, 'terms', id)) {
          termErrors.set(id, term.target);
        }
      }
      boldSpans(theirs).forEach(span => bold.add(span));
    }
    if (tagError) error(where, tagError);
    if (identical && !(typeof entries[key]?.identical === 'string' && entries[key].identical)) {
      error(where, `is the English text at ${identical} (untranslated); record an intentional identity in the review ledger ("identical")`);
    }
    if (literalError) error(where, literalError);
    if (fragment && !entries[key]?.identical) englishFragments.push(key);
    for (const [id, target] of termErrors) error(where, `names the concept '${id}' without the termbase's '${target}' (or a listed form); record an exception in the ledger ("terms")`);
    for (const [pair, word] of forbidden) error(where, `uses '${word}', a forbidden alternative for the concept '${pair.split(':')[0]}'`);
    for (const [id, concept] of Object.entries(CONCEPT)) {
      if (concept.key === key && TERMS[id] && message.trim() !== TERMS[id].target && !exempt(key, 'terms', id)) {
        error(where, `is the concept '${id}', which the termbase says as '${TERMS[id].target}'`);
      }
    }
    // Help: an element named by its displayed label shows that label's current translation.
    for (const control of controlsOf[key] || []) {
      const parts = control.keys.map(each => (sources[each] === sha(english.messages[each]) ? catalog.messages[each] : undefined));
      if (parts.some(part => typeof part !== 'string')) continue;            // a label still in debt: checked once translated
      const shown = new RegExp(`^${parts.map(part => escapeRe(part.trim())).join('\\s*[→←]\\s*')}${control.suffix ? '[\\s\\u00a0\\u202f]*[:：]?' : ''}$`, 'u');
      if (![...bold].some(span => shown.test(span)) && !exempt(key, 'labels', String(control.index))) {
        error(where, `the Help names the control ${control.keys.join(' → ')} (span ${control.index}), but no <b> span says its translation '${parts.join(' → ')}'`);
      }
    }
    // Review provenance, for the current English and the current text.
    const entry = entries[key];
    // A review counts for the translation as it is now, of the English as it is now.
    const reviewedNow = current && entry && entry.source === sha(english.messages[key]) && entry.translation === sha(message);
    for (const each of (reviewedNow ? entry.provenance : ['unrecorded'])) provenance[each] = (provenance[each] || 0) + 1;
    if (!reviewedNow || !entry.provenance.some(each => each === 'ai-review' || each === 'human-review')) unreviewed.push(key);
    if (isCritical(key) && !(reviewedNow && entry.provenance.some(each => each.endsWith('back-translation')))) noBackTranslation.push(key);
    for (const finding of (entry?.findings || [])) if (finding.status === 'open') openFindings.push({key, kind: finding.kind, note: finding.note});
  }
  for (const key of Object.keys(english.messages)) if (!(key in catalog.messages)) missing.push(key);
  const criticalMissing = missing.filter(isCritical), criticalStale = stale.filter(isCritical);
  const openCritical = openFindings.filter(finding => finding.kind === 'critical' || isCritical(finding.key));
  report.locales[tag] = {status: info.status, catalog: existsSync(path), missing: missing.length, stale: stale.length,
    critical: criticalMissing.length + criticalStale.length, criticalMissing: criticalMissing.length,
    criticalStale: criticalStale.length, missingKeys: missing, staleKeys: stale,
    criticalKeys: [...criticalMissing, ...criticalStale], criticalMissingKeys: criticalMissing,
    criticalStaleKeys: criticalStale,
    termbase: !!termbase, ledger: !!ledger, provenance,
    unreviewed: unreviewed.length, unreviewedKeys: unreviewed,
    noBackTranslation: noBackTranslation.length, noBackTranslationKeys: noBackTranslation,
    openFindings, openCritical: openCritical.length, englishFragmentKeys: englishFragments};
  if (args.has('--release') && info.status === 'supported' && (missing.length || stale.length)) {
    error(tag, `advertised as supported with ${missing.length} missing and ${stale.length} stale keys`);
  }
  // The candidate check of one locale (--complete) and the release check of a supported one.
  if (COMPLETE === tag || (args.has('--release') && info.status === 'supported')) {
    const what = COMPLETE === tag ? 'is not complete' : 'is advertised as supported but';
    if (COMPLETE === tag && (missing.length || stale.length)) error(tag, `${what}: ${missing.length} missing and ${stale.length} stale keys`);
    const absent = Object.keys(CONCEPT).filter(id => !TERMS[id]);
    if (absent.length) error(tag, `${what}: its termbase has no term for ${absent.join(', ')}`);
    if (unreviewed.length) error(tag, `${what}: ${unreviewed.length} translations without a current independent review`);
    if (noBackTranslation.length) error(tag, `${what}: ${noBackTranslation.length} critical keys without a current back-translation`);
    if (openFindings.length) error(tag, `${what}: ${openFindings.length} open review findings (${openCritical.length} critical)`);
  }
}
if (COMPLETE && !(COMPLETE in report.locales)) error(COMPLETE, '--complete names no target locale of the manifest');

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
    if (row.unreviewed || row.noBackTranslation || row.openFindings.length) {
      console.log(`WARNING review debt ${tag}: ${row.unreviewed} unreviewed, ${row.noBackTranslation} critical without back-translation, `
        + `${row.openFindings.length} open findings (${row.openCritical} critical)`);
    }
    if (row.englishFragmentKeys.length) console.log(`WARNING ${tag}: ${row.englishFragmentKeys.length} translations still hold English words`);
  }
  console.log(`${report.keys} English keys; ${errors.length} errors`);
}
// exitCode, not exit(): a large report written to a pipe is flushed first.
process.exitCode = errors.length ? 1 : 0;
