// The bilingual review sheet of one locale (WS5): every English key with its
// category, criticality, translator context and geometry note, the English
// and the translation (each plural and select shown at its sample values), the
// Help's control references the translation must show, and the review ledger's
// provenance, back-translation and findings. Text only: every cell is escaped.
//
//   node .github/localization/review_sheet.mjs --locale=TAG --out=FILE.html
//        [--i18n=DIR] [--meta=FILE] [--source=DIR] [--labels=FILE] [--terms=FILE]
//
// Writes FILE.html and, beside it, FILE.csv (the same rows). It reads what the
// validator reads and certifies nothing: the sheet is for people to review.
import {readFileSync, writeFileSync, existsSync} from 'node:fs';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import {render, samples, readable, sampleName} from './messages.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const option = name => process.argv.slice(2).find(arg => arg.startsWith(`--${name}=`))?.split('=').slice(1).join('=');
const TAG = option('locale'), OUT = option('out');
if (!TAG || !OUT || !OUT.endsWith('.html')) { console.error('usage: review_sheet.mjs --locale=TAG --out=FILE.html'); process.exit(2); }
const I18N = option('i18n') || join(ROOT, 'frontend', 'static', 'i18n');
const META = option('meta') || join(ROOT, 'frontend', 'i18n', 'en.meta.json');
const SOURCE = option('source') || dirname(META);
const LABELS = option('labels') || join(ROOT, 'frontend', 'i18n', 'en.labels.json');
const read = path => JSON.parse(readFileSync(path, 'utf8'));
const maybe = path => (existsSync(path) ? read(path) : null);
const sha = text => createHash('sha256').update(text).digest('hex').slice(0, 16);

const english = read(join(I18N, 'en.json')).messages;
const meta = read(META).meta;
const catalog = maybe(join(I18N, `${TAG}.json`)) || {messages: {}, sources: {}};
const ledger = maybe(join(SOURCE, 'review', `${TAG}.json`))?.entries || {};
const spans = maybe(LABELS)?.spans || {};

// Each sample a message reads differently at, rendered for people.
function said(message, tag, key) {
  if (typeof message !== 'string') return [];
  const seen = new Map();
  for (const values of samples(english[key], meta[key]?.args)) {
    let text;
    try { text = readable(render(message, tag, values)); } catch (e) { text = `(does not format: ${e.message})`; }
    if (!seen.has(text)) seen.set(text, sampleName(values));
  }
  return [...seen].map(([text, at]) => (at ? `[${at}] ${text}` : text));
}

const rows = Object.keys(english).sort().map(key => {
  const m = meta[key] || {}, entry = ledger[key] || {};
  const translation = catalog.messages[key];
  const status = translation === undefined ? 'missing'
    : catalog.sources?.[key] !== sha(english[key]) ? 'stale' : 'current';
  const controls = (spans[key] || []).filter(span => span.kind === 'control')
    .map(span => (span.keys || [span.key]).map(each => `${each} → ${catalog.messages[each] ?? '(untranslated)'}`).join(' · '));
  return {
    key, category: m.category || '', criticality: m.criticality || '', context: m.context || '', geometry: m.geometry || '',
    english: said(english[key], 'en', key).join('\n'), translation: said(translation, TAG, key).join('\n'), status,
    controls: controls.join('\n'),
    provenance: (entry.provenance || []).join(', '),
    reviewer: entry.human ? `${entry.human.reviewer} (${entry.human.evidence})` : '',
    back: entry.backTranslation || '',
    findings: (entry.findings || []).map(f => `[${f.status}] ${f.kind}: ${f.note}${f.resolution ? ` → ${f.resolution}` : ''}`).join('\n'),
    exemptions: [entry.identical && `identical: ${entry.identical}`,
      ...Object.entries(entry.terms || {}).map(([id, why]) => `term ${id}: ${why}`),
      ...Object.entries(entry.labels || {}).map(([i, why]) => `label ${i}: ${why}`)].filter(Boolean).join('\n'),
  };
});

const COLUMNS = ['key', 'category', 'criticality', 'context', 'geometry', 'english', 'translation', 'status', 'controls',
  'provenance', 'reviewer', 'back', 'findings', 'exemptions'];
const escape = text => String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const csvCell = text => (/[",\n]/.test(text) ? `"${String(text).replace(/"/g, '""')}"` : String(text));
const counts = rows.reduce((all, row) => ({...all, [row.status]: (all[row.status] || 0) + 1}), {});

const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Review sheet ${escape(TAG)}</title>
<style>body{font:13px system-ui,sans-serif;margin:16px}table{border-collapse:collapse}td,th{border:1px solid #bbb;padding:4px;vertical-align:top;white-space:pre-wrap}
tr.critical-warning td{background:#fff3f0}th{position:sticky;top:0;background:#eee}</style></head><body>
<h1>Review sheet: ${escape(TAG)}</h1>
<p>${rows.length} keys: ${escape(Object.entries(counts).map(([k, v]) => `${v} ${k}`).join(', '))}. Provenance is as recorded in the review ledger; this sheet certifies nothing.</p>
<table><thead><tr>${COLUMNS.map(c => `<th>${escape(c)}</th>`).join('')}</tr></thead><tbody>
${rows.map(row => `<tr class="${escape(row.criticality)}">${COLUMNS.map(c => `<td>${escape(row[c])}</td>`).join('')}</tr>`).join('\n')}
</tbody></table></body></html>
`;
writeFileSync(OUT, html);
writeFileSync(OUT.replace(/\.html$/, '.csv'), [COLUMNS.join(','), ...rows.map(row => COLUMNS.map(c => csvCell(row[c])).join(','))].join('\n') + '\n');
console.log(`${OUT}: ${rows.length} keys (${Object.entries(counts).map(([k, v]) => `${v} ${k}`).join(', ')})`);
