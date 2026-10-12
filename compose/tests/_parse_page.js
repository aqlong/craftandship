// Parses an HTML file in headless Chromium with JavaScript off, and prints what
// the parsed DOM holds as one JSON object: how many <nav> elements it has, the
// text of <body> without its script and style elements, and the text of the
// first JSON-LD script element. Used by test_layout_jsonld_parse.py.
//
// Usage: node _parse_page.js <playwright package directory> <html file>
// Exit status 3 means Chromium is not installed for that Playwright package.
const fs = require('fs');

const BROWSER_UNAVAILABLE = 3;
const [playwrightDir, htmlPath] = process.argv.slice(2);

(async () => {
  const { chromium } = require(playwrightDir);
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
  } catch (error) {
    console.error('BROWSER_UNAVAILABLE: ' + error.message.split('\n')[0]);
    process.exit(BROWSER_UNAVAILABLE);
  }
  try {
    const context = await browser.newContext({ javaScriptEnabled: false });
    const page = await context.newPage();
    await page.setContent(fs.readFileSync(htmlPath, 'utf-8'));
    const parsed = await page.evaluate(() => {
      const body = document.body.cloneNode(true);
      body.querySelectorAll('script, style').forEach((element) => element.remove());
      const ld = document.querySelector('script[type="application/ld+json"]');
      return {
        navCount: document.querySelectorAll('nav').length,
        bodyText: body.textContent,
        ldJsonText: ld ? ld.textContent : null,
      };
    });
    process.stdout.write(JSON.stringify(parsed));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
