// Drives an emitted services/health.js against a local stand-in for the
// site's home page and prints what it observed as JSON.
//
//   node _health_check_probe.js <path to services/health.js>
//
// The stand-in is an http server on a loopback port chosen by the system.
// Its answer for "/" is switched by the script: a status, or "hang" (accept
// the request and never answer).
'use strict';

const http = require('http');
const { createHomeCheck } = require(process.argv[2]);

let mode = 500;
let hits = 0;
const pending = [];
const server = http.createServer(function (req, res) {
  hits += 1;
  if (mode === 'hang') { pending.push(res); return; }
  res.statusCode = mode;
  res.end('home');
});

function sleep(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }

(async function () {
  const realError = console.error;
  const logged = [];
  console.error = function () { logged.push(Array.prototype.join.call(arguments, ' ')); };
  await new Promise(function (resolve) { server.listen(0, '127.0.0.1', resolve); });
  const port = server.address().port;
  const out = {};

  // 1. Before start() nothing has been asked and the answer is "not ok".
  const check = createHomeCheck({ timeoutMs: 400, retryAfterMs: 150 });
  out.beforeStart = await check.ok();
  out.hitsBeforeStart = hits;

  // 2. The home page answers 500: the first check fails, and a probe that
  //    comes right after does not ask again.
  mode = 500;
  out.startWith500 = await check.start(port);
  out.okRightAfterFailure = await check.ok();
  out.hitsAfterFailure = hits;

  // 3. Once the retry gap has passed a probe asks again; the page now
  //    answers 200 and the check passes.
  await sleep(200);
  mode = 200;
  const together = await Promise.all([check.ok(), check.ok(), check.ok()]);
  out.okAfterRecovery = together;
  out.hitsAfterRecovery = hits;

  // 4. A pass is remembered: the page breaks again, probes still say ok
  //    and no further request is made.
  mode = 500;
  out.okAfterLaterBreak = [await check.ok(), await check.ok()];
  out.hitsAfterLaterBreak = hits;

  // 5. A home page that never answers fails within the timeout.
  const slow = createHomeCheck({ timeoutMs: 300, retryAfterMs: 0 });
  mode = 'hang';
  const began = Date.now();
  out.hangResult = await slow.start(port);
  out.hangTookMs = Date.now() - began;

  // 6. 404 and 302 are answers of a working site; 503 is not.
  const statuses = {};
  for (const status of [200, 302, 404, 499, 500, 503]) {
    const one = createHomeCheck({ timeoutMs: 400, retryAfterMs: 0 });
    mode = status;
    statuses[status] = await one.start(port);
  }
  out.byStatus = statuses;

  // 7. Nothing listening at all.
  const nobody = createHomeCheck({ timeoutMs: 400, retryAfterMs: 0 });
  out.closedPort = await nobody.start(1);

  out.logged = logged;
  console.error = realError;
  pending.forEach(function (res) { res.destroy(); });
  server.close();
  process.stdout.write(JSON.stringify(out));
  process.exit(0);
})();
