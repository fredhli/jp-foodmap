// `node --test tests/worker/` entry point. The suite itself lives in run.mjs
// (which is also runnable on its own: `node tests/worker/run.mjs`).
import test from 'node:test';
import assert from 'node:assert';
import {runAll} from './run.mjs';

const {results} = await runAll();
for (const r of results) {
  test(r.id + ' — ' + r.name, () => {
    assert.ok(r.pass, r.detail || 'assertion failed');
  });
}
