// Rendering a catalog message at sample values, for the catalog validator
// (validate.mjs) and the bilingual review sheet (review_sheet.mjs): the one
// place both take their sample values and rendering from. Uses the vendored
// formatter the interface uses (frontend/static/vendor/intl-messageformat).
import {readFileSync} from 'node:fs';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const VENDOR = join(ROOT, 'frontend', 'static', 'vendor', 'intl-messageformat.esm.js');
export const {IntlMessageFormat} = await import('data:text/javascript;base64,' + Buffer.from(readFileSync(VENDOR, 'utf8')).toString('base64'));

export const RICH_TAGS = new Set(['b', 'strong', 'em', 'i', 'code', 'kbd']);
export const TYPE = {LITERAL: 0, ARGUMENT: 1, NUMBER: 2, DATE: 3, TIME: 4, SELECT: 5, PLURAL: 6, POUND: 7, TAG: 8};
// The counts every plural is shown at (task WS5: 0 1 2 3 5 11 21 101 and a fraction).
export const COUNTS = [0, 1, 2, 3, 5, 11, 21, 101, 1.5];
const OTHER = 'other';                 // a select value no case names
const formatLocale = tag => (tag.endsWith('-XA') || tag.endsWith('-XB') ? 'en' : tag);
const MARK = (name, close) => `${close ? '/' : ''}${name}`;
const TAG_VALUES = Object.fromEntries([...RICH_TAGS].map(name => [name, chunks => MARK(name) + chunks.join('') + MARK(name, true)]));

const formatters = new Map();
// A message said at `values`, its rich-text tags marked (b … /b).
export function render(message, tag, values) {
  const id = `${tag}\0${message}`;
  if (!formatters.has(id)) formatters.set(id, new IntlMessageFormat(message, formatLocale(tag)));
  const out = formatters.get(id).format({...values, ...TAG_VALUES});
  return Array.isArray(out) ? out.join('') : String(out);
}

// The argument values a key is rendered at, from its English: each count for a
// count (a plural, a number, or an argument its metadata declares a number),
// each case of a select and one no case names, a marked placeholder otherwise.
// Every argument is shown at every one of its values where it is said: an
// argument inside a select case is varied with that case chosen, one inside a
// plural's branch with each of that plural's counts, so a nested branch is
// rendered, not merely listed. The rest keep their first value. Bounded: one
// row per value per place an argument is said (times the counts of at most
// two enclosing plurals), never the full product of every argument.
export function samples(englishMessage, argsMeta = {}) {
  const values = {}, places = [];
  const walk = (elements, path) => elements.forEach(element => {
    if (element.type === TYPE.SELECT) {
      values[element.value] = [...new Set([...(values[element.value] || []), ...Object.keys(element.options).filter(o => o !== 'other'), OTHER])];
    } else if (element.type === TYPE.PLURAL || element.type === TYPE.NUMBER) values[element.value] = COUNTS;
    else if (element.type === TYPE.ARGUMENT && !(element.value in values)) {
      values[element.value] = /^(number|plural)\b/.test(String(argsMeta[element.value] || '')) ? COUNTS : [`\u27e8${element.value}\u27e9`];
    } else if (element.type === TYPE.DATE || element.type === TYPE.TIME) values[element.value] = [new Date(Date.UTC(2026, 0, 2, 3, 4, 5))];
    if (element.type in {1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1}) places.push({name: element.value, path});
    if (element.type === TYPE.TAG) walk(element.children, path);
    if (element.type === TYPE.SELECT || element.type === TYPE.PLURAL) {
      for (const [option, branch] of Object.entries(element.options)) {
        walk(branch.value, [...path, {name: element.value, kind: element.type === TYPE.SELECT ? 'select' : 'plural', option}]);
      }
    }
  });
  walk(new IntlMessageFormat(englishMessage, 'en').getAst(), []);
  const base = Object.fromEntries(Object.entries(values).map(([name, list]) => [name, list[0]]));
  const rows = new Map();
  const add = row => rows.set(JSON.stringify(Object.entries(row).map(([k, v]) => [k, v instanceof Date ? v.toISOString() : v])), row);
  add(base);
  for (const {name, path} of places) {
    // A select case on the way is chosen; a plural on the way is shown at each of its counts.
    const fixed = Object.fromEntries(path.filter(step => step.kind === 'select').map(step => [step.name, step.option === 'other' ? OTHER : step.option]));
    const counted = [...new Set(path.filter(step => step.kind === 'plural' && step.name !== name).map(step => step.name))].slice(-2);
    let around = [{}];
    for (const outer of counted) around = around.flatMap(row => COUNTS.map(count => ({...row, [outer]: count})));
    for (const row of around) for (const value of values[name]) add({...base, ...fixed, ...row, [name]: value});
  }
  return [...rows.values()];
}

export const plainText = text => text.replace(/\/?[a-z]+/g, '');
// A rendering for people: tags as <b>…</b>, placeholders as {name}.
export const readable = text => text.replace(/(\/?)([a-z]+)/g, '<$1$2>').replace(/⟨([^⟩]*)⟩/g, '{$1}');
export const boldSpans = text => [...text.matchAll(/b([\s\S]*?)\/b/g)].map(m => plainText(m[1]).trim());
export const sampleName = values => Object.entries(values).map(([name, value]) => `${name}=${String(value).replace(/[⟨⟩]/g, '')}`).join(', ');
export const isPlaceholder = /⟨[^⟩]*⟩|other/g;
