// Runs the web app's real spec translator on a list of extracted specs.
//
//   node _translate_extracted_spec.js <web dir> <inputs.json>
//
// <inputs.json> holds a JSON array of extracted specs (the shape the spec
// extraction returns). Standard output is a JSON array with one entry per
// input: {spec, warnings} as translateExtractedSpecToV1 returned it, or
// {error} when it threw.
//
// The translator is one TypeScript file without imports, so it is compiled
// here, in memory, by the TypeScript compiler from <web dir>/node_modules.
// Nothing is written to disk and no build of the web app is needed.
const fs = require('fs');
const path = require('path');

const webDir = process.argv[2];
const inputsPath = process.argv[3];
const ts = require(path.join(webDir, 'node_modules', 'typescript'));
const sourcePath = path.join(webDir, 'src', 'services', 'harbor_to_compose.ts');
const compiled = ts.transpileModule(fs.readFileSync(sourcePath, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
}).outputText;
const translator = { exports: {} };
new Function('exports', 'require', 'module', compiled)(translator.exports, require, translator);

const inputs = JSON.parse(fs.readFileSync(inputsPath, 'utf8'));
const results = inputs.map((input) => {
  try {
    return translator.exports.translateExtractedSpecToV1(input);
  } catch (err) {
    return { error: String(err && err.message) };
  }
});
process.stdout.write(JSON.stringify(results));
