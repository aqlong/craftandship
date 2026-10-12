// Renders widget templates on their own, many per process.
//
//   node _render_widgets.js <jobs.json> <out.json>
//
// jobs = [{ id, file, data }]: `file` is a widget template (absolute path),
// `data` the locals it is given. A small set of locals every page has
// (settings, csrfToken, cspNonce, currentPath, user) is added when absent.
//
// For each job the result is { id, ok, error, reads }. `reads` lists every
// name the template looked up among its locals, whether or not the name
// was there: the template runs inside `with (locals)`, so a Proxy around
// the locals sees each free identifier. Names that resolve to a JavaScript
// global are left out.
'use strict';

const fs = require('fs');
const path = require('path');
const ejs = require('ejs');

const jobs = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const compiled = {};

function pageLocals() {
  return {
    settings: {
      site_name: 'Sample Site', site_description: 'A sample.', contact_email: 'owner@example.com',
      contact_phone: '555-0100', _palette_vars: {}, _fonts: { display: {}, body: {} },
      _fonts_url: '', _field_types: {}, _us_states: [],
    },
    csrfToken: 'csrf', cspNonce: 'nonce', currentPath: '/', user: null,
  };
}

const results = [];
for (const job of jobs) {
  const reads = new Set();
  let ok = true;
  let error = null;
  try {
    if (!compiled[job.file]) {
      compiled[job.file] = ejs.compile(fs.readFileSync(job.file, 'utf8'), {
        filename: job.file,
        // include('widgets/<dir>/<name>') resolves the way it does in a
        // built site, where compose/widgets is views/widgets.
        views: [path.resolve(path.dirname(job.file), '..', '..')],
      });
    }
    const data = Object.assign(pageLocals(), job.data || {});
    const traced = new Proxy(data, {
      has: function (target, key) {
        if (typeof key === 'string' && !(key in globalThis)) reads.add(key);
        return key in target;
      },
    });
    compiled[job.file](traced);
  } catch (e) {
    ok = false;
    error = String(e && e.message).split('\n').filter(function (l) { return l.trim(); }).slice(-1)[0];
  }
  results.push({ id: job.id, ok: ok, error: error, reads: Array.from(reads).sort() });
}
fs.writeFileSync(process.argv[3], JSON.stringify(results));
