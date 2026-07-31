// Tests the test runner, not the application.
//
// task_authoring_harness.js is only worth anything if it can actually go red.
// The version this replaced could not: it printed "!! UNHANDLED REJECTION",
// counted it, and still exited 0 — a green suite over a broken screen, the
// exact "unreadable dressed as clean" failure the screen's own code is written
// to avoid. So the runner's failure policy is itself a thing under test.
//
// Each scenario runs in its OWN child process, because the whole point is to
// observe an EXIT CODE, and because deliberate failures must never leak into
// the real run's tally.
//
// Usage: node runner_selftest.js <path-to-index.html>
'use strict';

const path = require('path');
const {spawnSync} = require('child_process');

const HTML_PATH = process.argv[2];
if (!HTML_PATH) {
  console.error('usage: node runner_selftest.js <path-to-index.html>');
  process.exit(2);
}
const HARNESS = path.join(__dirname, 'task_authoring_harness.js');

const SCENARIOS = [
  {
    scenario: 'clean',
    why: 'a run with nothing wrong must exit 0 — otherwise "red" means nothing',
    expectZero: true,
    expectStdout: /cases passed, no async errors/,
  },
  {
    scenario: 'reject',
    why: 'a detached rejected promise must exit non-zero',
    expectZero: false,
    expectStderr: /UNHANDLED REJECTION/,
    forbidStdout: /cases passed, no async errors/,
  },
  {
    scenario: 'throw',
    why: 'a callback that throws outside any await must exit non-zero',
    expectZero: false,
    expectStderr: /UNCAUGHT EXCEPTION/,
    forbidStdout: /cases passed, no async errors/,
  },
  {
    scenario: 'assert',
    why: 'an ordinary expectation mismatch must exit non-zero',
    expectZero: false,
    expectStdout: /\[FAIL\] selftest: an ordinary assertion mismatch/,
    forbidStdout: /cases passed, no async errors/,
  },
  {
    scenario: 'casethrow',
    why: 'a case whose body throws is a FAILED case, never an absent one',
    expectZero: false,
    expectStdout: /\[FAIL\] selftest: a case that throws/,
    forbidStdout: /cases passed, no async errors/,
  },
  {
    scenario: 'hang',
    why: 'a never-settling await must not end the run at exit 0 mid-suite',
    expectZero: false,
    expectStderr: /RUN DID NOT FINISH/,
    forbidStdout: /cases passed, no async errors/,
  },
  {
    scenario: 'crash',
    why: 'a failure outside every case still exits non-zero via main().catch',
    expectZero: false,
    expectStderr: /HARNESS CRASHED/,
    forbidStdout: /cases passed, no async errors/,
  },
];

let failures = 0;
const rows = [];

for (const s of SCENARIOS) {
  const run = spawnSync(
    process.execPath, [HARNESS, HTML_PATH, `--selftest=${s.scenario}`],
    {encoding: 'utf8', timeout: 60000});
  const code = run.status;
  const problems = [];
  if (s.expectZero && code !== 0) problems.push(`expected exit 0, got ${code}`);
  if (!s.expectZero && (code === 0 || code === null)) {
    problems.push(`expected a non-zero exit, got ${code}`);
  }
  if (s.expectStdout && !s.expectStdout.test(run.stdout)) {
    problems.push(`stdout missing ${s.expectStdout}`);
  }
  if (s.expectStderr && !s.expectStderr.test(run.stderr)) {
    problems.push(`stderr missing ${s.expectStderr}`);
  }
  if (s.forbidStdout && s.forbidStdout.test(run.stdout)) {
    problems.push(`stdout still claims a clean run (${s.forbidStdout})`);
  }
  if (problems.length) failures++;
  rows.push({scenario: s.scenario, code, problems, why: s.why});
}

for (const r of rows) {
  console.log(`\n[${r.problems.length ? 'FAIL' : 'PASS'}] --selftest=${r.scenario} `
    + `→ exit ${r.code}`);
  console.log(`  ${r.why}`);
  for (const p of r.problems) console.log(`  ✗ ${p}`);
}

console.log(`\n${rows.length - failures}/${rows.length} runner self-tests passed`);
process.exitCode = failures === 0 ? 0 : 1;
