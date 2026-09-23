/**
 * ShiftBoard Automated Test Suite
 * Tests domain models, working calendar conversions, constraint checkers,
 * heuristic forward-scheduling, scenario transitions, and export generators.
 */

const assert = require('assert');
const engine = require('./engine.js');

console.log('====================================================');
console.log('Running ShiftBoard Automated Verification Suite');
console.log('====================================================\n');

let passedTests = 0;
let totalTests = 0;

function runTest(name, fn) {
  totalTests++;
  try {
    fn();
    console.log(`  ✓ ${name}`);
    passedTests++;
  } catch (err) {
    console.error(`  ✗ ${name}`);
    console.error(`    ${err.message}`);
    throw err;
  }
}

// 1. Calendar & Time Slot Conversion Tests
runTest('Calendar: timeToSlot and slotToTime roundtrip consistency', () => {
  // Monday 08:00 should be slot 0
  const slot0 = engine.timeToSlot(0, 8, 0);
  assert.strictEqual(slot0, 0, 'Mon 08:00 must be slot 0');

  const t0 = engine.slotToTime(0);
  assert.strictEqual(t0.dayIndex, 0);
  assert.strictEqual(t0.dayName, 'Mon');
  assert.strictEqual(t0.hour, 8);
  assert.strictEqual(t0.minute, 0);
  assert.strictEqual(t0.timeStr, '08:00');

  // Tuesday 13:00 should be slot 18 + (13-8)*2 = 18 + 10 = 28
  const slotTue13 = engine.timeToSlot(1, 13, 0);
  assert.strictEqual(slotTue13, 28, 'Tue 13:00 must be slot 28');
  const tTue13 = engine.slotToTime(28);
  assert.strictEqual(tTue13.dayName, 'Tue');
  assert.strictEqual(tTue13.timeStr, '13:00');

  // Friday 16:30 should be slot 4*18 + (16-8)*2 + 1 = 72 + 16 + 1 = 89 (last slot)
  const slotFriEnd = engine.timeToSlot(4, 16, 30);
  assert.strictEqual(slotFriEnd, 89, 'Fri 16:30 must be slot 89');
  const tFriEnd = engine.slotToTime(89);
  assert.strictEqual(tFriEnd.dayName, 'Fri');
  assert.strictEqual(tFriEnd.timeStr, '16:30');

  // slotToTimeEnd test: slot 89 ends at Fri 17:00
  const tFriClose = engine.slotToTimeEnd(89);
  assert.strictEqual(tFriClose.dayName, 'Fri');
  assert.strictEqual(tFriClose.timeStr, '17:00');

  // Friday 17:00 (End of Shift) should correctly map to slot 89
  const slotFri17 = engine.timeToSlot(4, 17, 0);
  assert.strictEqual(slotFri17, 89, 'Fri 17:00 (End of Shift) must map to slot 89');
  const tFriClose17 = engine.slotToTimeEnd(slotFri17);
  assert.strictEqual(tFriClose17.displayStr, 'Fri 17:00');

  // Monday 17:00 should map to slot 17 (shift end of day 0)
  const slotMon17 = engine.timeToSlot(0, 17, 0);
  assert.strictEqual(slotMon17, 17, 'Mon 17:00 must map to slot 17');
  const tMonClose17 = engine.slotToTimeEnd(slotMon17);
  assert.strictEqual(tMonClose17.displayStr, 'Mon 17:00');
});

runTest('Calendar: formatSlotRange handles shifts & multi-day spans', () => {
  const range1 = engine.formatSlotRange(0, 3); // Mon 08:00 to 10:00 (4 slots = 2h)
  assert(range1.includes('Mon 08:00'));
  assert(range1.includes('Mon 10:00'));
  assert(range1.includes('2.0h'));

  // Mon 14:00 (slot 12) to Tue 11:00 (slot 23) -> 12 slots = 6.0h
  const range2 = engine.formatSlotRange(12, 23);
  assert(range2.includes('Mon 14:00'));
  assert(range2.includes('Tue 11:00'));
  assert(range2.includes('6.0h'));
});

// 2. Default Workshop Baseline State Validation
runTest('Baseline State: pristine workshop has 0 conflicts and 100% on-time rate', () => {
  const ws = engine.getDefaultWorkshop();
  assert.strictEqual(ws.machines.length, 5, 'Must have 5 machines');
  assert.strictEqual(ws.jobs.length, 8, 'Must have 8 customer jobs');
  assert.strictEqual(ws.operations.length, 21, 'Must have 21 operations');
  assert.strictEqual(ws.outages.length, 1, 'Must have 1 planned outage (OUT-01 on M1)');

  const conflicts = engine.validateSchedule(ws);
  assert.strictEqual(conflicts.length, 0, 'Baseline schedule must have 0 conflicts');

  const kpis = engine.calculateKPIs(ws);
  assert.strictEqual(kpis.totalJobs, 8);
  assert.strictEqual(kpis.onTimeJobsCount, 8);
  assert.strictEqual(kpis.lateJobsCount, 0);
  assert.strictEqual(kpis.onTimeRatePercent, 100);
  assert.strictEqual(kpis.unscheduledOperationsCount, 0);
  assert(kpis.overallUtilizationPercent > 0, 'Utilization must be positive');
});

// 3. Conflict Detection Engine Tests
runTest('Conflict Engine: detects machine capacity overlap', () => {
  const ws = engine.getDefaultWorkshop();
  // Move OP-103-1 on M3 (currently slots 0..9) to collide with OP-102-2 on M3 (slots 10..17)
  const op = ws.operations.find(o => o.id === 'OP-103-1');
  op.startSlot = 8;
  op.endSlot = 17; // Overlaps with OP-102-2 on M3

  const conflicts = engine.validateSchedule(ws);
  const overlapConflict = conflicts.find(c => c.type === 'machine_overlap');
  assert(overlapConflict, 'Must detect machine_overlap conflict');
  assert.strictEqual(overlapConflict.machineId, 'M3');
  assert(overlapConflict.description.includes('OP-103-1') || overlapConflict.description.includes('OP-102-2'));
});

runTest('Conflict Engine: detects planned outage collision', () => {
  const ws = engine.getDefaultWorkshop();
  // On M1, outage is Tue 13:00-17:00 (slots 28..35)
  // Move OP-101-2 to slots 27..34 on M1
  const op = ws.operations.find(o => o.id === 'OP-101-2');
  op.startSlot = 27;
  op.endSlot = 34;

  const conflicts = engine.validateSchedule(ws);
  const outageConflict = conflicts.find(c => c.type === 'outage_overlap');
  assert(outageConflict, 'Must detect outage_overlap conflict');
  assert.strictEqual(outageConflict.machineId, 'M1');
  assert.strictEqual(outageConflict.outageId, 'OUT-01');
});

runTest('Conflict Engine: detects operation precedence violations', () => {
  const ws = engine.getDefaultWorkshop();
  // In JOB-101: OP-101-1 is seq 10 (ends slot 3), OP-101-2 is seq 20 (starts slot 4)
  // Move OP-101-2 earlier to slot 2 (starts before OP-101-1 finishes)
  const op2 = ws.operations.find(o => o.id === 'OP-101-2');
  op2.startSlot = 2;
  op2.endSlot = 9;

  const conflicts = engine.validateSchedule(ws);
  const precConflict = conflicts.find(c => c.type === 'precedence_violation');
  assert(precConflict, 'Must detect precedence violation');
  assert.strictEqual(precConflict.jobId, 'JOB-101');
  assert.strictEqual(precConflict.operationId, 'OP-101-2');
  assert.strictEqual(precConflict.predecessorId, 'OP-101-1');
});

runTest('Conflict Engine: detects customer order lateness and tardiness', () => {
  const ws = engine.getDefaultWorkshop();
  // JOB-103 due date is slot 44 (Wed 12:00)
  // Move OP-103-2 (last op of JOB-103) to slots 46..51
  const lastOp = ws.operations.find(o => o.id === 'OP-103-2');
  lastOp.startSlot = 46;
  lastOp.endSlot = 51;

  const conflicts = engine.validateSchedule(ws);
  const lateConflict = conflicts.find(c => c.type === 'tardiness');
  assert(lateConflict, 'Must detect tardiness warning');
  assert.strictEqual(lateConflict.jobId, 'JOB-103');

  const kpis = engine.calculateKPIs(ws);
  assert.strictEqual(kpis.lateJobsCount, 1);
  assert(kpis.totalTardinessHours > 0);
});

runTest('Conflict Engine: preserves duration on horizon boundary and flags out_of_bounds conflict', () => {
  const ws = engine.getDefaultWorkshop();
  // Find a 2.5h operation (5 slots), e.g. OP-104-1 (setup 0.5h + run 2.0h = 2.5h = 5 slots)
  const op = ws.operations.find(o => o.id === 'OP-104-1');
  assert(op, 'OP-104-1 must exist');
  const durationSlots = Math.round(((op.setupHours || 0) + (op.runHours || 0)) * 2);
  assert.strictEqual(durationSlots, 5, 'OP-104-1 must be 5 slots (2.5h)');

  // Move OP-104-1 to start slot 86 on M2 (extends to slot 90, exceeding slot 89 horizon)
  const movedState = engine.moveOperation(ws, 'OP-104-1', 'M2', 86);
  const movedOp = movedState.operations.find(o => o.id === 'OP-104-1');

  // Verify duration is not silently truncated to 2.0h / 4 slots
  assert.strictEqual(movedOp.startSlot, 86, 'startSlot must be 86');
  assert.strictEqual(movedOp.endSlot, 90, 'endSlot must be 90 (5 slots duration preserved, not clamped to 89)');
  const renderedSlots = movedOp.endSlot - movedOp.startSlot + 1;
  assert.strictEqual(renderedSlots, 5, 'Rendered block duration must be 5 slots (2.5h)');

  // Validate schedule flags out_of_bounds conflict
  const conflicts = engine.validateSchedule(movedState);
  const boundConflict = conflicts.find(c => c.type === 'out_of_bounds' && c.operationId === 'OP-104-1');
  assert(boundConflict, 'Must detect out_of_bounds critical conflict for operation extending past slot 89');
  assert.strictEqual(boundConflict.severity, 'critical');
  assert(boundConflict.description.includes('OP-104-1') || boundConflict.description.includes('exceeds'));
});

// 4. Heuristic Forward Scheduling Engine
runTest('Heuristic Scheduler: proposes valid schedule respecting priorities & outages', () => {
  const ws = engine.getDefaultWorkshop();
  // Mark all operations as unscheduled
  ws.operations.forEach(op => {
    op.isUnscheduled = true;
    op.startSlot = null;
    op.endSlot = null;
  });

  const proposal = engine.proposeSchedule(ws);
  assert(proposal.newState, 'Proposal must return newState');
  assert(proposal.changes.length > 0, 'Changes must be recorded');

  // Verify proposed schedule has zero critical conflicts
  const conflicts = engine.validateSchedule(proposal.newState);
  const critical = conflicts.filter(c => c.severity === 'critical');
  assert.strictEqual(critical.length, 0, 'Heuristic proposal must have zero critical conflicts');

  // Verify planned outage on M1 was not violated
  const m1Ops = proposal.newState.operations.filter(o => o.machineId === 'M1' && !o.isUnscheduled);
  m1Ops.forEach(op => {
    // OUT-01 is slots 28..35
    const overlapsOutage = Math.max(op.startSlot, 28) <= Math.min(op.endSlot, 35);
    assert(!overlapsOutage, `Operation ${op.id} on M1 must not overlap with outage (slots 28..35)`);
  });

  // Verify precedence is preserved for all jobs
  proposal.newState.jobs.forEach(job => {
    const jOps = proposal.newState.operations.filter(o => o.jobId === job.id && !o.isUnscheduled);
    jOps.sort((a, b) => a.seq - b.seq);
    for (let k = 1; k < jOps.length; k++) {
      assert(jOps[k].startSlot > jOps[k - 1].endSlot, `Job ${job.id}: Op seq ${jOps[k].seq} must start after Op seq ${jOps[k-1].seq}`);
    }
  });
});

runTest('Heuristic Scheduler: deterministic tie-breaker sorts by job ID when priority and due date match', () => {
  const state = {
    machines: [{ id: 'M1', name: 'Mill 1', shortName: 'M1', code: 'M1', workCenter: 'Mill', color: '#2563eb' }],
    jobs: [
      { id: 'JOB-B', customer: 'Cust B', partName: 'Part B', priority: 'NORMAL', dueDateSlot: 50 },
      { id: 'JOB-A', customer: 'Cust A', partName: 'Part A', priority: 'NORMAL', dueDateSlot: 50 }
    ],
    operations: [
      { id: 'OP-B-1', jobId: 'JOB-B', seq: 10, name: 'Op B1', machineId: 'M1', setupHours: 0, runHours: 1, isUnscheduled: true },
      { id: 'OP-A-1', jobId: 'JOB-A', seq: 10, name: 'Op A1', machineId: 'M1', setupHours: 0, runHours: 1, isUnscheduled: true }
    ],
    outages: []
  };

  const prop = engine.proposeSchedule(state);
  const opA = prop.newState.operations.find(o => o.id === 'OP-A-1');
  const opB = prop.newState.operations.find(o => o.id === 'OP-B-1');
  // JOB-A should be scheduled before JOB-B due to localeCompare('JOB-A', 'JOB-B') < 0
  assert(opA.startSlot < opB.startSlot, 'JOB-A should be scheduled before JOB-B based on deterministic job ID tie-breaker');
});

// 5. Scenarios (Urgent Order, Extended Outage & Undo / Restore)
runTest('Scenario: Simulate Urgent Order introduces competition and resolves cleanly', () => {
  let ws = engine.getDefaultWorkshop();
  const res = engine.applyUrgentOrderScenario(ws);
  assert(res.applied, 'Urgent order scenario must be applied');
  ws = res.newState;

  assert(ws.jobs.some(j => j.id === 'JOB-901'), 'JOB-901 must be added');
  assert(ws.operations.some(o => o.id === 'OP-901-1'), 'OP-901-1 must be added');

  // Urgent order creates intentional conflict on board
  const conflictsBefore = engine.validateSchedule(ws);
  assert(conflictsBefore.length > 0, 'Urgent order must create conflicts against baseline');

  // Running autoResolveConflicts / proposeSchedule resolves all conflicts
  const resolved = engine.autoResolveConflicts(ws);
  const conflictsAfter = engine.validateSchedule(resolved.newState);
  const criticalAfter = conflictsAfter.filter(c => c.severity === 'critical');
  assert.strictEqual(criticalAfter.length, 0, 'Auto-resolve must clear all critical conflicts');
});

runTest('Scenario: Simulate Extended Outage triggers conflict and can be resolved', () => {
  let ws = engine.getDefaultWorkshop();
  const res = engine.applyExtendedOutageScenario(ws);
  assert(res.applied, 'Extended outage scenario must be applied');
  ws = res.newState;

  const m1Outage = ws.outages.find(o => o.machineId === 'M1');
  assert(m1Outage.endSlot >= 44, 'Outage must be extended through at least slot 44');

  const conflicts = engine.validateSchedule(ws);
  assert(conflicts.length > 0, 'Extended outage must trigger conflicts');

  // Auto-resolve clears critical conflicts
  const resolved = engine.autoResolveConflicts(ws);
  const critAfter = engine.validateSchedule(resolved.newState).filter(c => c.severity === 'critical');
  assert.strictEqual(critAfter.length, 0, 'Auto-resolve must clear critical conflicts');
});

// 6. Exports & Serialization
runTest('Exports: Markdown and CSV reports are properly generated', () => {
  const ws = engine.getDefaultWorkshop();
  const md = engine.generateMarkdownExport(ws);
  assert(typeof md === 'string', 'Markdown must be a string');
  assert(md.includes('# ShiftBoard Production Schedule & Dispatch Report'));
  assert(md.includes('Haas UMC-750'));
  assert(md.includes('Apex Aerospace'));

  const csv = engine.generateCSVExport(ws);
  assert(typeof csv === 'string', 'CSV must be a string');
  assert(csv.includes('Job ID,Customer,Part Name,Priority,Job Due Date'));
  assert(csv.includes('JOB-101'));
  assert(csv.includes('OP-101-1'));
});

// 7. Security: Plan Schema Validation & Input Hardening (S03-F1)
runTest('Security: validatePlanSchema admits valid baseline workshop state', () => {
  const ws = engine.getDefaultWorkshop();
  const res = engine.validatePlanSchema(ws);
  assert(res.valid, 'Baseline workshop must pass schema validation');
  assert.strictEqual(res.errors.length, 0);
  assert(res.sanitizedState, 'Must produce clean sanitizedState');
  assert.strictEqual(res.sanitizedState.machines.length, 5);
  assert.strictEqual(res.sanitizedState.jobs.length, 8);
  assert.strictEqual(res.sanitizedState.operations.length, 21);
});

runTest('Security: validatePlanSchema rejects non-object or malformed input', () => {
  assert(!engine.validatePlanSchema(null).valid);
  assert(!engine.validatePlanSchema(undefined).valid);
  assert(!engine.validatePlanSchema('string').valid);
  assert(!engine.validatePlanSchema([1, 2, 3]).valid);
});

runTest('Security: validatePlanSchema rejects unknown top-level and entity fields', () => {
  const fs = require('fs');
  const path = require('path');
  const unknownFixture = JSON.parse(
    fs.readFileSync(path.join(__dirname, 'fixtures', 'invalid_schema_unknown_fields.json'), 'utf8')
  );
  const res = engine.validatePlanSchema(unknownFixture);
  assert(!res.valid, 'Plan with unknown fields must fail schema validation');
  assert(res.errors.some(e => e.includes('Unknown top-level field "injected_evil_field"')), 'Must flag unknown top-level field');
  assert(res.errors.some(e => e.includes('unknown field "unknown_machine_attr"')), 'Must flag unknown machine field');
});

runTest('Security: validatePlanSchema rejects invalid types, bad slots, invalid colors, and missing foreign keys', () => {
  const fs = require('fs');
  const path = require('path');
  const badTypesFixture = JSON.parse(
    fs.readFileSync(path.join(__dirname, 'fixtures', 'invalid_schema_bad_types.json'), 'utf8')
  );
  const res = engine.validatePlanSchema(badTypesFixture);
  assert(!res.valid, 'Plan with bad types must fail schema validation');
  assert(res.errors.some(e => e.includes('hourlyRate')), 'Must reject negative hourly rate');
  assert(res.errors.some(e => e.includes('color')), 'Must reject invalid color string');
  assert(res.errors.some(e => e.includes('quantity')), 'Must reject negative quantity');
  assert(res.errors.some(e => e.includes('dueDateSlot')), 'Must reject out-of-bounds due date slot');
  assert(res.errors.some(e => e.includes('priority')), 'Must reject invalid priority');
  assert(res.errors.some(e => e.includes('references non-existent job')), 'Must reject invalid job foreign key');
  assert(res.errors.some(e => e.includes('references non-existent machine')), 'Must reject invalid machine foreign key');
});

runTest('Security: validatePlanSchema admits valid XSS fixture, cleans attributes and provides inert strings', () => {
  const fs = require('fs');
  const path = require('path');
  const xssFixture = JSON.parse(
    fs.readFileSync(path.join(__dirname, 'fixtures', 'malicious_xss_plan.json'), 'utf8')
  );
  const res = engine.validatePlanSchema(xssFixture);
  assert(res.valid, `XSS fixture must satisfy valid structural schema: ${res.errors.join(', ')}`);
  assert(res.sanitizedState, 'Must return sanitized state');

  // Verify strings are preserved for safe textContent rendering
  const op = res.sanitizedState.operations[0];
  assert(op.name.includes('<svg onload='), 'String contents preserved');

  // Verify escapeHTML escapes all dangerous HTML metacharacters
  const escaped = engine.escapeHTML(op.name);
  assert(!escaped.includes('<'), 'Must escape <');
  assert(!escaped.includes('>'), 'Must escape >');
  assert(escaped.includes('&lt;'), 'Must encode &lt;');
  assert(escaped.includes('&gt;'), 'Must encode &gt;');

  // Verify sanitizeColor rejects script injection attempts
  assert.strictEqual(engine.sanitizeColor('red; background: url(javascript:...)'), '#2563eb');
  assert.strictEqual(engine.sanitizeColor('#2563eb'), '#2563eb');
});

// 8. Security: Server Binding to Loopback (S03-F2)
runTest('Security: run.sh binds python http.server strictly to 127.0.0.1', () => {
  const fs = require('fs');
  const path = require('path');
  const runScript = fs.readFileSync(path.join(__dirname, 'run.sh'), 'utf8');
  assert(
    runScript.includes('python3 -m http.server "$PORT" --bind 127.0.0.1'),
    'run.sh must invoke http.server with --bind 127.0.0.1'
  );
  assert(
    runScript.includes('Exposure model: Bound strictly to loopback interface (127.0.0.1)'),
    'run.sh must document the loopback exposure model'
  );
});

console.log(`\n====================================================`);
console.log(`All ${passedTests} / ${totalTests} ShiftBoard tests passed successfully!`);
console.log(`====================================================\n`);
