// t/v8check.js -- the external half of the validation oracle.
//
// Every claim this repository used to make about validity was self-scored:
// t/mkwasm.py decided which fixtures were well formed, t/run.sh checked that
// wasmi agreed with t/mkwasm.py, and nothing outside the repository had ever
// looked at a byte either of them produced.
//
// V8 is outside the repository.  It is a production WebAssembly engine written
// by people who have never seen this code, and `WebAssembly.validate` is its
// spec validator.  So: for every fixture, ask V8 whether the module is valid
// and instantiable, and require that answer to match what the .cases file
// declares.  t/run.sh separately requires wasmi to match the same declaration,
// so the two together are a comparison of wasmi against V8.
//
// The two directions are counted and reported SEPARATELY and never summed.
// They are different claims, and a validator that rejects everything would
// score a perfect 100% on the second one.
//
// This is not the spec testsuite (see PLAN.md Milestone 1): V8 is being asked
// about modules written here, not about the WebAssembly working group's own
// corpus.  It is an external authority on a local corpus, which is strictly
// more than this project had before and strictly less than `spec/test/core`.

const fs = require('fs');
const path = require('path');

const FIX = 't/fix';

// Modules V8 considers perfectly valid and wasmi refuses on purpose, because
// the feature is out of scope rather than because the module is broken.  This
// list is the `unimpl()` idea from METHOD.md: the external authority must
// ACCEPT the module, and we must refuse it cleanly.  A refusal with no outside
// confirmation that the input was good is indistinguishable from a bug.
const SCOPE_REFUSALS = {
  'rej_f32.wasm': 'f32 is not implemented; refused at load time',
  'rej_f64.wasm': 'f64 is not implemented; refused at load time',
};

// Pinned, like CASES_EXPECTED in t/run.sh: a corpus that quietly shrinks must
// turn the suite red rather than report a smaller perfect score.
const EXPECT = { accept: 10, reject: 56, scope: 2 };

let accept = 0, reject = 0, scope = 0;
const bad = [];

const files = fs.readdirSync(FIX).filter(f => f.endsWith('.wasm')).sort();
if (files.length === 0) {
  console.error('v8check: no fixtures in ' + FIX);
  process.exit(1);
}

for (const f of files) {
  const bytes = fs.readFileSync(path.join(FIX, f));
  const cases = fs.readFileSync(path.join(FIX, f.replace(/\.wasm$/, '.cases')), 'utf8');
  // The declaration under test: `!error` means wasmi must refuse the module.
  const declaredBad = /^!error\b/m.test(cases);

  // V8's verdict.  Validation and instantiation are separate stages in the
  // spec -- an out-of-bounds data segment is a valid module that cannot be
  // instantiated -- and wasmi collapses both into "error: ..., exit 2", so
  // the oracle has to run both stages to be comparable.
  let ok = WebAssembly.validate(bytes);
  let stage = 'valid', why = '';
  if (ok) {
    try {
      const mod = new WebAssembly.Module(bytes);
      if (WebAssembly.Module.imports(mod).length) {
        // No fixture has imports today.  If one ever does, instantiating it
        // against {} would fail for a reason that is about this harness and
        // not about the module, so say so instead of counting it.
        bad.push(f + ': has imports; v8check cannot instantiate it');
        continue;
      }
      new WebAssembly.Instance(mod, {});
    } catch (e) {
      ok = false;
      stage = e.constructor.name;
      why = e.message.split('\n')[0];
    }
  } else {
    stage = 'invalid';
    try { new WebAssembly.Module(bytes); } catch (e) { why = e.message.split('\n')[0]; }
  }

  if (f in SCOPE_REFUSALS) {
    if (!ok) bad.push(f + ': expected V8 to ACCEPT this (' + SCOPE_REFUSALS[f] +
                      ') but it said ' + stage + ': ' + why);
    else if (!declaredBad) bad.push(f + ': listed as an out-of-scope refusal but not declared !error');
    else scope++;
  } else if (declaredBad && ok) {
    bad.push(f + ': declared !error here, but V8 accepts it as valid WebAssembly');
  } else if (!declaredBad && !ok) {
    bad.push(f + ': declared well formed here, but V8 rejects it -- ' + stage + ': ' + why);
  } else if (ok) {
    accept++;
  } else {
    reject++;
  }
}

console.log('v8check: ' + files.length + ' modules through V8 ' + process.version);
console.log('  accept direction: ' + accept + ' modules V8 calls valid and wasmi runs');
console.log('  reject direction: ' + reject + ' modules V8 calls invalid and wasmi refuses');
console.log('  out of scope:     ' + scope + ' modules V8 calls valid and wasmi refuses on purpose');

for (const b of bad) console.log('  DISAGREE ' + b);

let rc = bad.length ? 1 : 0;
for (const k of Object.keys(EXPECT)) {
  const got = { accept, reject, scope }[k];
  if (got !== EXPECT[k]) {
    console.log('  v8check: ' + k + ' direction has ' + got + ' modules, pinned at ' +
                EXPECT[k] + '; update EXPECT in t/v8check.js if the corpus grew on purpose');
    rc = 1;
  }
}
process.exit(rc);
