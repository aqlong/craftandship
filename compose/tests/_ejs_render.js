const fs = require('fs');
const path = require('path');
let ejs;
try { ejs = require('ejs'); }
catch (e) {
  try {
    const alt = process.env.NODE_PATH || path.join(process.cwd(), 'builds/sample-healer/node_modules');
    ejs = require(path.join(alt, 'ejs'));
  } catch (e2) { console.error('ejs module not available'); process.exit(2); }
}
const widgetPath = process.argv[2];
const data = JSON.parse(process.argv[3] || '{}');
// Stub csrfToken + settings that widgets commonly reference
if (!data.csrfToken) data.csrfToken = 'test-csrf';
if (!data.settings) data.settings = {
  site_name: 'Test Site', site_description: 'desc', contact_email: 'a@b.c',
  _palette_vars: {'--color-primary':'#6b1519','--color-on-primary':'#fff'},
  _fonts: {display: {family:'Rubik'}, body: {family:'Nunito Sans'}},
  _fonts_url: 'https://fonts.googleapis.com/css2?...'
};
try {
  const src = fs.readFileSync(widgetPath, 'utf-8');
  // `views` lets a widget write include('widgets/<dir>/<name>') the way the
  // generated app does (compose/ stands in for the app's views/ tree).
  const out = ejs.render(src, data, { filename: widgetPath, views: [path.resolve(__dirname, '..')] });
  process.stdout.write(out);
} catch (e) {
  console.error(e.message); process.exit(1);
}
