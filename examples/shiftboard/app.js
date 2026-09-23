/**
 * ShiftBoard Application Controller
 * UI orchestration, interactive Gantt board, drag-and-drop,
 * conflict visualization, undo/redo history, and modal managers.
 */

(function () {
  'use strict';

  // Reference core engine
  const engine = window.ShiftBoardEngine;
  if (!engine) {
    console.error('ShiftBoardEngine not loaded.');
    return;
  }

  // --- Application State ---
  let state = engine.getDefaultWorkshop();
  let selectedOpId = null;
  let activeTab = 'inspector';
  let hoveredJobId = null;
  let isDrawerOpen = true;

  // History stack for Undo / Redo
  const undoStack = [];
  const redoStack = [];
  const MAX_HISTORY = 40;

  // Track initial baseline for quick comparison
  const baselineState = engine.cloneState(state);

  // --- DOM Elements Cache ---
  const DOM = {
    // Buttons
    btnPropose: document.getElementById('btn-propose'),
    btnUndo: document.getElementById('btn-undo'),
    btnRedo: document.getElementById('btn-redo'),
    btnScenarioUrgent: document.getElementById('btn-scenario-urgent'),
    btnScenarioOutage: document.getElementById('btn-scenario-outage'),
    btnRestoreDemo: document.getElementById('btn-restore-demo'),
    btnSavePlan: document.getElementById('btn-save-plan'),
    btnLoadPlan: document.getElementById('btn-load-plan'),
    fileInput: document.getElementById('file-input'),
    btnExport: document.getElementById('btn-export'),
    btnHelp: document.getElementById('btn-help'),
    btnKpiInfo: document.getElementById('btn-kpi-info'),
    btnToggleDrawer: document.getElementById('btn-toggle-drawer'),
    btnCloseDrawer: document.getElementById('btn-close-drawer'),

    // KPI Displays
    kpiUtilizationVal: document.getElementById('kpi-utilization-val'),
    kpiUtilizationFill: document.getElementById('kpi-utilization-fill'),
    kpiOnTimeVal: document.getElementById('kpi-ontime-val'),
    kpiOnTimeSubtext: document.getElementById('kpi-ontime-subtext'),
    kpiLateVal: document.getElementById('kpi-late-val'),
    kpiLateSubtext: document.getElementById('kpi-late-subtext'),
    kpiConflictsVal: document.getElementById('kpi-conflicts-val'),
    kpiConflictsSubtext: document.getElementById('kpi-conflicts-subtext'),
    kpiUnscheduledVal: document.getElementById('kpi-unscheduled-val'),

    // Notification Banner
    notificationBanner: document.getElementById('notification-banner'),
    notificationText: document.getElementById('notification-text'),
    btnNotificationAction: document.getElementById('btn-notification-action'),
    notificationActionText: document.getElementById('notification-action-text'),
    notificationActionIcon: document.getElementById('notification-action-icon'),
    btnNotificationUndo: document.getElementById('btn-notification-undo'),
    btnNotificationDismiss: document.getElementById('btn-notification-dismiss'),

    // Schedule Board
    scheduleContainer: document.getElementById('schedule-container'),
    scheduleGrid: document.getElementById('schedule-grid'),

    // Side Drawer & Tabs
    sideDrawer: document.getElementById('side-drawer'),
    drawerTabs: document.querySelectorAll('.drawer-tab'),
    drawerBody: document.getElementById('drawer-body'),
    badgeUnscheduledCount: document.getElementById('badge-unscheduled-count'),
    badgeConflictsCount: document.getElementById('badge-conflicts-count'),

    // Modals
    modalHelp: document.getElementById('modal-help'),
    modalKpi: document.getElementById('modal-kpi'),
    modalExport: document.getElementById('modal-export'),
    modalAddJob: document.getElementById('modal-add-job'),
    modalAddOutage: document.getElementById('modal-add-outage')
  };

  // --- History (Undo/Redo) Management ---
  function pushState(actionLabel) {
    undoStack.push({
      label: actionLabel,
      state: engine.cloneState(state)
    });
    if (undoStack.length > MAX_HISTORY) {
      undoStack.shift();
    }
    // Clear redo on new action
    redoStack.length = 0;
    updateUndoRedoButtons();
  }

  function handleUndo() {
    if (undoStack.length === 0) return;
    const currentSnapshot = {
      label: 'Prior state',
      state: engine.cloneState(state)
    };
    redoStack.push(currentSnapshot);

    const prev = undoStack.pop();
    state = prev.state;
    updateUndoRedoButtons();
    showNotification(`Undid: ${prev.label}`, false);
    renderAll();
  }

  function handleRedo() {
    if (redoStack.length === 0) return;
    const currentSnapshot = {
      label: 'Prior state',
      state: engine.cloneState(state)
    };
    undoStack.push(currentSnapshot);

    const next = redoStack.pop();
    state = next.state;
    updateUndoRedoButtons();
    showNotification(`Redid: ${next.label}`, false);
    renderAll();
  }

  function updateUndoRedoButtons() {
    DOM.btnUndo.disabled = (undoStack.length === 0);
    DOM.btnRedo.disabled = (redoStack.length === 0);
    if (undoStack.length > 0) {
      DOM.btnUndo.title = `Undo: ${undoStack[undoStack.length - 1].label} (Ctrl+Z)`;
    } else {
      DOM.btnUndo.title = 'Nothing to Undo (Ctrl+Z)';
    }
    if (redoStack.length > 0) {
      DOM.btnRedo.title = `Redo: ${redoStack[redoStack.length - 1].label} (Ctrl+Y)`;
    } else {
      DOM.btnRedo.title = 'Nothing to Redo (Ctrl+Y)';
    }
  }

  // --- Notifications ---
  function showNotification(text, isWarning, actionCallback, actionText = 'Auto-Resolve', actionIcon = '⚡') {
    DOM.notificationText.textContent = text;
    DOM.notificationBanner.className = 'notification-banner' + (isWarning ? ' warning' : '');
    DOM.notificationBanner.style.display = 'flex';

    if (DOM.btnNotificationAction) {
      if (actionCallback) {
        DOM.btnNotificationAction.style.display = 'inline-flex';
        if (DOM.notificationActionText) DOM.notificationActionText.textContent = actionText;
        if (DOM.notificationActionIcon) DOM.notificationActionIcon.textContent = actionIcon;
        DOM.btnNotificationAction.onclick = () => {
          hideNotification();
          actionCallback();
        };
      } else {
        DOM.btnNotificationAction.style.display = 'none';
      }
    }

    if (DOM.btnNotificationUndo) {
      if (undoStack.length > 0) {
        DOM.btnNotificationUndo.style.display = 'inline-flex';
        DOM.btnNotificationUndo.onclick = () => {
          hideNotification();
          handleUndo();
        };
      } else {
        DOM.btnNotificationUndo.style.display = 'none';
      }
    }
  }

  function hideNotification() {
    DOM.notificationBanner.style.display = 'none';
  }

  // --- Render Schedule Board ---
  function renderScheduleBoard() {
    const conflicts = engine.validateSchedule(state);
    const kpis = engine.calculateKPIs(state);
    const grid = DOM.scheduleGrid;
    grid.innerHTML = '';

    // 1. Corner Header (Machine / Time)
    const corner = document.createElement('div');
    corner.className = 'schedule-corner-header';
    corner.innerHTML = `
      <div style="font-size: 11px; text-transform: uppercase; color: var(--text-muted); font-weight: 700; letter-spacing: 0.05em;">Workstation</div>
      <div style="font-size: 13px; font-weight: 800; color: #fff;">Machine / Timeline</div>
    `;
    grid.appendChild(corner);

    // 2. Days Header Row (Row 1)
    engine.DAYS.forEach((day, dayIdx) => {
      const dayCell = document.createElement('div');
      dayCell.className = 'day-header-cell';
      // span 18 slots (columns 2 + dayIdx*18 to 2 + (dayIdx+1)*18)
      const colStart = 2 + dayIdx * engine.SLOTS_PER_DAY;
      const colSpan = engine.SLOTS_PER_DAY;
      dayCell.style.gridColumn = `${colStart} / span ${colSpan}`;
      dayCell.style.gridRow = '1';
      dayCell.innerHTML = `
        <span>${engine.DAY_LABELS[dayIdx]}</span>
        <span style="font-size: 10px; color: var(--text-secondary); font-family: monospace;">08:00 – 17:00 (9h)</span>
      `;
      grid.appendChild(dayCell);
    });

    // 3. Hours Header Row (Row 2)
    for (let slot = 0; slot < engine.TOTAL_SLOTS; slot++) {
      const t = engine.slotToTime(slot);
      const hourCell = document.createElement('div');
      const isHourStart = (t.minute === 0);
      const isDayBoundary = (slot % engine.SLOTS_PER_DAY === engine.SLOTS_PER_DAY - 1);

      hourCell.className = 'hour-header-cell' +
        (isHourStart ? ' hour-start' : '') +
        (isDayBoundary ? ' day-boundary' : '');
      hourCell.style.gridColumn = `${2 + slot}`;
      hourCell.style.gridRow = '2';

      if (isHourStart) {
        hourCell.textContent = String(t.hour).padStart(2, '0');
      } else {
        hourCell.textContent = '·';
      }
      hourCell.title = `${t.dayName} ${t.timeStr}`;
      grid.appendChild(hourCell);
    }

    // 4. Machine Rows
    state.machines.forEach((machine, mIdx) => {
      const rowIndex = 3 + mIdx;
      const machKpi = kpis.machineKPIs[machine.id] || { utilizationPercent: 0, scheduledHours: 0 };

      // Machine Row Label (Sticky Left)
      const machineLabel = document.createElement('div');
      machineLabel.className = 'machine-row-label';
      machineLabel.style.gridColumn = '1';
      machineLabel.style.gridRow = `${rowIndex}`;

      const titleDiv = document.createElement('div');
      titleDiv.className = 'machine-name-title';

      const colorDot = document.createElement('span');
      colorDot.style.width = '8px';
      colorDot.style.height = '8px';
      colorDot.style.borderRadius = '50%';
      colorDot.style.backgroundColor = engine.sanitizeColor(machine.color);
      colorDot.style.display = 'inline-block';
      titleDiv.appendChild(colorDot);

      const nameSpan = document.createElement('span');
      nameSpan.textContent = ' ' + machine.shortName + ' ';
      titleDiv.appendChild(nameSpan);

      const badge = document.createElement('span');
      badge.className = 'machine-badge';
      badge.textContent = machine.code;
      titleDiv.appendChild(badge);
      machineLabel.appendChild(titleDiv);

      const subtext = document.createElement('div');
      subtext.className = 'machine-subtext';
      subtext.textContent = machine.workCenter;
      machineLabel.appendChild(subtext);

      const meter = document.createElement('div');
      meter.className = 'machine-util-meter';
      meter.title = `Utilization: ${machKpi.utilizationPercent}% (${machKpi.totalScheduledHours}h / ${machKpi.availableHours}h)`;

      const trackEl = document.createElement('div');
      trackEl.className = 'machine-util-track';
      const fillEl = document.createElement('div');
      fillEl.className = 'machine-util-fill';
      fillEl.style.width = `${Math.min(100, Math.max(0, machKpi.utilizationPercent))}%`;
      trackEl.appendChild(fillEl);
      meter.appendChild(trackEl);

      const utilText = document.createElement('span');
      utilText.className = 'machine-util-text';
      utilText.textContent = `${machKpi.utilizationPercent}%`;
      meter.appendChild(utilText);
      machineLabel.appendChild(meter);

      grid.appendChild(machineLabel);

      // Timeline Track
      const track = document.createElement('div');
      track.className = 'timeline-track';
      track.style.gridColumn = `2 / span ${engine.TOTAL_SLOTS}`;
      track.style.gridRow = `${rowIndex}`;
      track.dataset.machineId = machine.id;

      // Track background cells (for drag-drop snap targets)
      for (let s = 0; s < engine.TOTAL_SLOTS; s++) {
        const cell = document.createElement('div');
        const isDayBoundary = (s % engine.SLOTS_PER_DAY === engine.SLOTS_PER_DAY - 1);
        cell.className = 'track-cell' + (isDayBoundary ? ' day-boundary' : '');
        cell.dataset.slot = s;
        cell.dataset.machineId = machine.id;

        // Drag & Drop events on cell
        cell.addEventListener('dragover', handleCellDragOver);
        cell.addEventListener('dragleave', handleCellDragLeave);
        cell.addEventListener('drop', handleCellDrop);

        track.appendChild(cell);
      }

      // Render Planned Outages on this machine
      const machineOutages = state.outages.filter(o => o.machineId === machine.id);
      machineOutages.forEach(outage => {
        const outageEl = document.createElement('div');
        outageEl.className = 'schedule-outage';
        const start = outage.startSlot;
        const slotsCount = outage.endSlot - outage.startSlot + 1;
        const leftPx = start * 48; // var(--time-col-width) is 48px
        const widthPx = slotsCount * 48 - 4;

        outageEl.style.left = `${leftPx}px`;
        outageEl.style.width = `${widthPx}px`;

        const headerDiv = document.createElement('div');
        headerDiv.className = 'outage-header';
        const wrenchSpan = document.createElement('span');
        wrenchSpan.textContent = '🔧 OUTAGE: ';
        const titleSpan = document.createElement('span');
        titleSpan.textContent = outage.title;
        headerDiv.appendChild(wrenchSpan);
        headerDiv.appendChild(titleSpan);
        outageEl.appendChild(headerDiv);

        const descDiv = document.createElement('div');
        descDiv.className = 'outage-desc';
        descDiv.textContent = `${engine.formatSlotRange(outage.startSlot, outage.endSlot)} · ${outage.reason}`;
        outageEl.appendChild(descDiv);

        outageEl.title = `Machine Outage: ${outage.title} (${engine.formatSlotRange(outage.startSlot, outage.endSlot)})\n${outage.reason}`;
        outageEl.addEventListener('click', () => {
          showNotification(`Outage selected: ${outage.title} on ${machine.shortName} (${engine.formatSlotRange(outage.startSlot, outage.endSlot)})`, false);
        });

        track.appendChild(outageEl);
      });

      // Render Scheduled Operations on this machine
      const machineOps = state.operations.filter(
        o => o.machineId === machine.id && !o.isUnscheduled && o.startSlot !== null && o.startSlot >= 0
      );

      machineOps.forEach(op => {
        const job = state.jobs.find(j => j.id === op.jobId) || {};
        const priorityMeta = engine.PRIORITIES[job.priority] || engine.PRIORITIES.NORMAL;
        const opConflicts = conflicts.filter(c => c.operationId === op.id || c.conflictingOperationId === op.id);
        const hasConflict = opConflicts.length > 0;

        const start = op.startSlot;
        const slotsCount = op.endSlot - op.startSlot + 1;
        const leftPx = start * 48;
        const widthPx = slotsCount * 48 - 4;

        const item = document.createElement('div');
        item.className = 'schedule-item' +
          (hasConflict ? ' has-conflict' : '') +
          (selectedOpId === op.id ? ' selected' : '');
        item.id = `item-${op.id}`;
        item.style.left = `${leftPx}px`;
        item.style.width = `${widthPx}px`;
        item.style.backgroundColor = engine.sanitizeColor(job.color);
        item.draggable = true;
        item.dataset.opId = op.id;
        item.dataset.jobId = op.jobId;

        // Setup time stripe
        const setupSlots = op.setupSlots || Math.round((op.setupHours || 0) * 2);
        if (setupSlots > 0) {
          const setupStripe = document.createElement('div');
          setupStripe.className = 'op-setup-stripe';
          setupStripe.style.width = `${setupSlots * 48}px`;
          setupStripe.title = `Setup: ${op.setupHours}h (Slots 1–${setupSlots})`;
          item.appendChild(setupStripe);
        }

        // Spanning shift boundary check
        const startDay = Math.floor(op.startSlot / engine.SLOTS_PER_DAY);
        const endDay = Math.floor(op.endSlot / engine.SLOTS_PER_DAY);
        if (endDay > startDay) {
          const spanBadge = document.createElement('div');
          spanBadge.className = 'op-continuation-badge';
          if (endDay >= engine.DAYS.length) {
            spanBadge.textContent = '⚠ Over Horizon';
            spanBadge.style.backgroundColor = '#ef4444';
            spanBadge.title = `Operation extends past Friday 17:00 (critical horizon conflict).`;
          } else {
            spanBadge.textContent = `Cont. ${engine.DAYS[endDay]}`;
            spanBadge.title = `Operation pauses overnight at 17:00 ${engine.DAYS[startDay]} and resumes at 08:00 ${engine.DAYS[endDay]}.`;
          }
          item.appendChild(spanBadge);
        }

        // Contents
        const headerLine = document.createElement('div');
        headerLine.className = 'op-header-line';

        const idBadge = document.createElement('span');
        idBadge.className = 'op-id-badge';
        idBadge.textContent = `${priorityMeta.icon} ${op.jobId} #${op.seq}`;
        headerLine.appendChild(idBadge);

        const statusBadge = document.createElement('span');
        statusBadge.className = 'op-status-badge';
        if (hasConflict) {
          statusBadge.style.backgroundColor = '#ef4444';
          statusBadge.textContent = '⚠ CONFLICT';
        } else if (op.endSlot > job.dueDateSlot) {
          const lateHours = ((op.endSlot - job.dueDateSlot) * 0.5).toFixed(1);
          statusBadge.style.backgroundColor = '#f59e0b';
          statusBadge.textContent = `LATE +${lateHours}h`;
        } else {
          statusBadge.style.backgroundColor = 'rgba(0,0,0,0.4)';
          statusBadge.textContent = 'ON TIME';
        }
        headerLine.appendChild(statusBadge);
        item.appendChild(headerLine);

        const titleLine = document.createElement('div');
        titleLine.className = 'op-title-line';
        titleLine.title = op.name || '';
        titleLine.textContent = op.name || '';
        item.appendChild(titleLine);

        const metaLine = document.createElement('div');
        metaLine.className = 'op-meta-line';
        const custSpan = document.createElement('span');
        custSpan.textContent = job.customer || '';
        const hoursSpan = document.createElement('span');
        hoursSpan.textContent = `${(op.setupHours || 0) > 0 ? '🔧' + op.setupHours + 'h + ' : ''}${op.runHours}h`;
        metaLine.appendChild(custSpan);
        metaLine.appendChild(hoursSpan);
        item.appendChild(metaLine);

        // Interactive events
        item.addEventListener('click', (e) => {
          e.stopPropagation();
          selectOperation(op.id);
        });

        // Sister operation hover effect
        item.addEventListener('mouseenter', () => {
          hoverJob(op.jobId);
        });
        item.addEventListener('mouseleave', () => {
          clearHoverJob();
        });

        // Drag events
        item.addEventListener('dragstart', handleItemDragStart);
        item.addEventListener('dragend', handleItemDragEnd);

        track.appendChild(item);
      });

      grid.appendChild(track);
    });
  }

  // --- Sister Highlighting on Hover ---
  function hoverJob(jobId) {
    hoveredJobId = jobId;
    const items = document.querySelectorAll('.schedule-item');
    items.forEach(item => {
      if (item.dataset.jobId === jobId) {
        item.classList.add('sister-highlight');
        item.classList.remove('sister-dimmed');
      } else {
        item.classList.add('sister-dimmed');
        item.classList.remove('sister-highlight');
      }
    });
  }

  function clearHoverJob() {
    hoveredJobId = null;
    const items = document.querySelectorAll('.schedule-item');
    items.forEach(item => {
      item.classList.remove('sister-highlight');
      item.classList.remove('sister-dimmed');
    });
  }

  // --- Drag and Drop Handlers ---
  let draggedOpId = null;

  function handleItemDragStart(e) {
    draggedOpId = this.dataset.opId;
    e.dataTransfer.setData('text/plain', draggedOpId);
    e.dataTransfer.effectAllowed = 'move';
    this.style.opacity = '0.5';
  }

  function handleItemDragEnd() {
    this.style.opacity = '1';
    draggedOpId = null;
    document.querySelectorAll('.track-cell').forEach(c => {
      c.classList.remove('drag-over-valid', 'drag-over-invalid');
    });
  }

  function handleCellDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const slot = parseInt(this.dataset.slot, 10);
    if (draggedOpId) {
      const op = state.operations.find(o => o.id === draggedOpId);
      if (op) {
        const slots = op.totalSlots || Math.round(((op.setupHours || 0) + (op.runHours || 0)) * engine.SLOTS_PER_HOUR);
        if (slot + slots > engine.TOTAL_SLOTS) {
          this.classList.add('drag-over-invalid');
          this.classList.remove('drag-over-valid');
          return;
        }
      }
    }
    this.classList.add('drag-over-valid');
    this.classList.remove('drag-over-invalid');
  }

  function handleCellDragLeave() {
    this.classList.remove('drag-over-valid', 'drag-over-invalid');
  }

  function handleCellDrop(e) {
    e.preventDefault();
    this.classList.remove('drag-over-valid', 'drag-over-invalid');

    const opId = e.dataTransfer.getData('text/plain') || draggedOpId;
    if (!opId) return;

    const targetMachineId = this.dataset.machineId;
    const targetStartSlot = parseInt(this.dataset.slot, 10);

    const op = state.operations.find(o => o.id === opId);
    if (!op) return;

    // Check if slot or machine changed
    if (op.machineId === targetMachineId && op.startSlot === targetStartSlot) {
      return;
    }

    pushState(`Move ${op.name} (${op.id}) to ${engine.slotToTime(targetStartSlot).displayStr}`);

    state = engine.moveOperation(state, opId, targetMachineId, targetStartSlot);
    selectOperation(opId);

    const conflicts = engine.validateSchedule(state);
    const horizonConflict = conflicts.find(c => c.operationId === opId && c.type === 'out_of_bounds');
    if (horizonConflict) {
      showNotification(`⚠ Horizon Violation: ${op.id} extends past Friday 17:00. Critical conflict flagged. Move earlier or unschedule.`, true);
    } else if (conflicts.length > 0) {
      showNotification(`Moved ${op.id} to ${engine.slotToTime(targetStartSlot).displayStr}. ⚠ Warning: Created ${conflicts.length} conflict(s).`, true);
    } else {
      showNotification(`Moved ${op.id} to ${targetMachineId} starting at ${engine.slotToTime(targetStartSlot).displayStr}.`, false);
    }

    renderAll();
  }

  // --- Operation Selection & Inspector ---
  function selectOperation(opId) {
    selectedOpId = opId;
    activeTab = 'inspector';
    renderDrawerTabs();
    renderDrawerContent();

    // Scroll slightly if needed or highlight
    document.querySelectorAll('.schedule-item').forEach(it => {
      if (it.dataset.opId === opId) {
        it.classList.add('selected');
      } else {
        it.classList.remove('selected');
      }
    });
  }

  // --- KPI Ribbon Updates ---
  function updateKPIRibbon() {
    const kpis = engine.calculateKPIs(state);

    // 1. Utilization
    DOM.kpiUtilizationVal.textContent = `${kpis.overallUtilizationPercent}%`;
    DOM.kpiUtilizationFill.style.width = `${Math.min(100, kpis.overallUtilizationPercent)}%`;

    // 2. On-Time Delivery
    DOM.kpiOnTimeVal.textContent = `${kpis.onTimeRatePercent}%`;
    DOM.kpiOnTimeSubtext.textContent = `${kpis.onTimeJobsCount} / ${kpis.totalJobs} jobs on time`;
    if (kpis.onTimeRatePercent === 100) {
      DOM.kpiOnTimeVal.className = 'kpi-value kpi-badge-ok';
    } else if (kpis.onTimeRatePercent >= 75) {
      DOM.kpiOnTimeVal.className = 'kpi-value kpi-badge-warn';
    } else {
      DOM.kpiOnTimeVal.className = 'kpi-value kpi-badge-crit';
    }

    // 3. Late Orders
    DOM.kpiLateVal.textContent = `${kpis.lateJobsCount}`;
    DOM.kpiLateSubtext.textContent = kpis.lateJobsCount > 0 ? `+${kpis.totalTardinessHours}h tardiness` : 'Zero overdue orders';
    DOM.kpiLateVal.className = kpis.lateJobsCount > 0 ? 'kpi-value kpi-badge-crit' : 'kpi-value kpi-badge-ok';

    // 4. Conflicts
    DOM.kpiConflictsVal.textContent = `${kpis.totalConflictsCount}`;
    DOM.kpiConflictsSubtext.textContent = kpis.totalConflictsCount > 0
      ? `${kpis.criticalConflictsCount} critical, ${kpis.warningConflictsCount} warning`
      : 'All constraints met';
    DOM.kpiConflictsVal.className = kpis.totalConflictsCount > 0 ? 'kpi-value kpi-badge-crit' : 'kpi-value kpi-badge-ok';

    // 5. Unscheduled
    DOM.kpiUnscheduledVal.textContent = `${kpis.unscheduledOperationsCount}`;
    DOM.badgeUnscheduledCount.textContent = kpis.unscheduledOperationsCount;
    DOM.badgeConflictsCount.textContent = kpis.totalConflictsCount;
  }

  // --- Side Drawer Tabs & Content ---
  function renderDrawerTabs() {
    DOM.drawerTabs.forEach(t => {
      if (t.dataset.tab === activeTab) {
        t.classList.add('active');
      } else {
        t.classList.remove('active');
      }
    });
  }

  function renderDrawerContent() {
    const container = DOM.drawerBody;
    container.innerHTML = '';

    if (activeTab === 'inspector') {
      renderInspectorTab(container);
    } else if (activeTab === 'unscheduled') {
      renderUnscheduledTab(container);
    } else if (activeTab === 'conflicts') {
      renderConflictsTab(container);
    } else if (activeTab === 'jobs') {
      renderJobsTab(container);
    } else if (activeTab === 'machines') {
      renderMachinesTab(container);
    }
  }

  // --- Tab 1: Inspector Tab ---
  function renderInspectorTab(container) {
    if (!selectedOpId) {
      container.innerHTML = `
        <div style="text-align: center; padding: 40px 20px; color: var(--text-muted);">
          <div style="font-size: 36px; margin-bottom: 8px;">🔍</div>
          <p style="font-weight: 600; color: var(--text-secondary);">No Operation Selected</p>
          <p style="font-size: 12px; margin-top: 4px;">Click on any operation block in the schedule or select from the unscheduled list to inspect details and edit parameters.</p>
        </div>
      `;
      return;
    }

    const op = state.operations.find(o => o.id === selectedOpId);
    if (!op) {
      selectedOpId = null;
      renderInspectorTab(container);
      return;
    }

    const job = state.jobs.find(j => j.id === op.jobId) || {};
    const machine = state.machines.find(m => m.id === op.machineId) || {};
    const pMeta = engine.PRIORITIES[job.priority] || engine.PRIORITIES.NORMAL;
    const conflicts = engine.validateSchedule(state).filter(c => c.operationId === op.id || c.conflictingOperationId === op.id);

    const card = document.createElement('div');
    card.className = 'inspector-card';

    const cardTitle = document.createElement('div');
    cardTitle.className = 'inspector-card-title';
    cardTitle.textContent = 'Operation Details';
    card.appendChild(cardTitle);

    const titleRow = document.createElement('div');
    titleRow.style.display = 'flex';
    titleRow.style.alignItems = 'center';
    titleRow.style.justifyContent = 'space-between';
    titleRow.style.marginBottom = '6px';

    const opNameH4 = document.createElement('h4');
    opNameH4.style.fontSize = '14px';
    opNameH4.style.color = '#fff';
    opNameH4.textContent = op.name;
    titleRow.appendChild(opNameH4);

    const opBadge = document.createElement('span');
    opBadge.className = 'op-id-badge';
    opBadge.style.backgroundColor = engine.sanitizeColor(job.color);
    opBadge.style.color = '#fff';
    opBadge.textContent = `${pMeta.icon} ${op.id}`;
    titleRow.appendChild(opBadge);
    card.appendChild(titleRow);

    const kvList = document.createElement('div');
    kvList.className = 'key-value-list';

    function addKV(k, v) {
      const kSpan = document.createElement('span');
      kSpan.className = 'key-label';
      kSpan.textContent = k;
      const vSpan = document.createElement('span');
      vSpan.className = 'key-val';
      vSpan.textContent = v;
      kvList.appendChild(kSpan);
      kvList.appendChild(vSpan);
    }

    addKV('Job ID:', `${job.id} (${job.partName || ''})`);
    addKV('Customer:', job.customer || '');
    addKV('Job Priority:', `${pMeta.icon} ${pMeta.label}`);
    addKV('Job Due Date:', engine.slotToTimeEnd(job.dueDateSlot).displayStr);
    addKV('Machine:', machine.name || op.machineId);
    addKV('Timing:', op.isUnscheduled ? '❌ Unscheduled' : engine.formatSlotRange(op.startSlot, op.endSlot));
    addKV('Duration:', `Setup: ${op.setupHours}h + Run: ${op.runHours}h = ${op.totalHours}h`);
    card.appendChild(kvList);

    // Conflict warnings for this op
    if (conflicts.length > 0) {
      const conflictBox = document.createElement('div');
      conflictBox.style.marginTop = '10px';
      conflictBox.style.padding = '8px 10px';
      conflictBox.style.backgroundColor = 'rgba(239, 68, 68, 0.15)';
      conflictBox.style.border = '1px solid #ef4444';
      conflictBox.style.borderRadius = 'var(--radius-sm)';

      const conflictHeader = document.createElement('div');
      conflictHeader.style.fontSize = '11px';
      conflictHeader.style.fontWeight = '700';
      conflictHeader.style.color = '#fca5a5';
      conflictHeader.style.marginBottom = '4px';
      conflictHeader.textContent = `⚠ ${conflicts.length} Active Conflict(s):`;
      conflictBox.appendChild(conflictHeader);

      const ul = document.createElement('ul');
      ul.style.paddingLeft = '16px';
      ul.style.fontSize = '11px';
      ul.style.color = '#fecaca';
      conflicts.forEach(c => {
        const li = document.createElement('li');
        li.textContent = c.description;
        ul.appendChild(li);
      });
      conflictBox.appendChild(ul);
      card.appendChild(conflictBox);
    }

    // Manual Re-scheduling Controls
    const controlsCard = document.createElement('div');
    controlsCard.className = 'inspector-card';

    const controlsTitle = document.createElement('div');
    controlsTitle.className = 'inspector-card-title';
    controlsTitle.textContent = 'Move & Reschedule Controls';
    controlsCard.appendChild(controlsTitle);

    const controlsBody = document.createElement('div');
    controlsBody.style.display = 'flex';
    controlsBody.style.flexDirection = 'column';
    controlsBody.style.gap = '8px';
    controlsBody.style.marginTop = '4px';

    const formGroup = document.createElement('div');
    formGroup.className = 'form-group';
    const machLabel = document.createElement('label');
    machLabel.className = 'form-label';
    machLabel.textContent = 'Assigned Machine';
    formGroup.appendChild(machLabel);

    const machSelect = document.createElement('select');
    machSelect.className = 'form-select';
    machSelect.id = 'inspector-machine-select';
    state.machines.forEach(m => {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.name;
      if (m.id === op.machineId) opt.selected = true;
      machSelect.appendChild(opt);
    });
    formGroup.appendChild(machSelect);
    controlsBody.appendChild(formGroup);

    const row1 = document.createElement('div');
    row1.style.display = 'flex';
    row1.style.gap = '8px';
    const btnEarlier = document.createElement('button');
    btnEarlier.className = 'btn btn-outline';
    btnEarlier.id = 'btn-move-earlier';
    btnEarlier.style.flex = '1';
    btnEarlier.textContent = '◀ -1.0h';
    if (op.isUnscheduled || op.startSlot <= 0) btnEarlier.disabled = true;
    const btnLater = document.createElement('button');
    btnLater.className = 'btn btn-outline';
    btnLater.id = 'btn-move-later';
    btnLater.style.flex = '1';
    btnLater.textContent = '+1.0h ▶';
    if (op.isUnscheduled || op.endSlot >= engine.TOTAL_SLOTS - 1) btnLater.disabled = true;
    row1.appendChild(btnEarlier);
    row1.appendChild(btnLater);
    controlsBody.appendChild(row1);

    const row2 = document.createElement('div');
    row2.style.display = 'flex';
    row2.style.gap = '8px';
    const btnToggleUnscheduled = document.createElement('button');
    btnToggleUnscheduled.className = 'btn btn-outline';
    btnToggleUnscheduled.id = 'btn-toggle-unscheduled';
    btnToggleUnscheduled.style.flex = '1';
    btnToggleUnscheduled.textContent = op.isUnscheduled ? '⚡ Schedule to Earliest' : '✕ Send to Unscheduled';
    row2.appendChild(btnToggleUnscheduled);
    controlsBody.appendChild(row2);

    controlsCard.appendChild(controlsBody);

    container.appendChild(card);
    container.appendChild(controlsCard);

    // Event handlers for controls
    machSelect.addEventListener('change', (e) => {
      const newMachId = e.target.value;
      pushState(`Change machine of ${op.id} to ${newMachId}`);
      op.machineId = newMachId;
      renderAll();
    });

    btnEarlier.addEventListener('click', () => {
      if (op.startSlot > 1) {
        pushState(`Shift ${op.id} -1 hour`);
        state = engine.moveOperation(state, op.id, op.machineId, op.startSlot - 2);
        renderAll();
      }
    });

    btnLater.addEventListener('click', () => {
      if (op.endSlot < engine.TOTAL_SLOTS - 2) {
        pushState(`Shift ${op.id} +1 hour`);
        state = engine.moveOperation(state, op.id, op.machineId, op.startSlot + 2);
        renderAll();
      }
    });

    btnToggleUnscheduled.addEventListener('click', () => {
      if (op.isUnscheduled) {
        pushState(`Schedule ${op.id}`);
        // Find earliest slot
        const prop = engine.proposeSchedule(state);
        state = prop.newState;
      } else {
        pushState(`Unschedule ${op.id}`);
        state = engine.moveOperation(state, op.id, op.machineId, -1);
      }
      renderAll();
    });
  }

  // --- Tab 2: Unscheduled Work Tab ---
  function renderUnscheduledTab(container) {
    const unscheduledOps = state.operations.filter(o => o.isUnscheduled);

    if (unscheduledOps.length === 0) {
      container.innerHTML = `
        <div style="text-align: center; padding: 40px 20px; color: var(--text-muted);">
          <div style="font-size: 36px; margin-bottom: 8px;">✅</div>
          <p style="font-weight: 600; color: var(--text-secondary);">All Operations are Scheduled</p>
          <p style="font-size: 12px; margin-top: 4px;">There is currently no displaced or unscheduled work in the queue.</p>
        </div>
      `;
      return;
    }

    container.innerHTML = `
      <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px;">
        <span style="font-size: 12px; font-weight: 700; color: var(--text-secondary);">${unscheduledOps.length} Operations in Queue</span>
        <button class="btn btn-primary btn-icon-only" id="btn-auto-place-all" title="Schedule All Unscheduled Work">⚡ Auto-Place All</button>
      </div>
      <div class="unscheduled-list"></div>
    `;

    const list = container.querySelector('.unscheduled-list');
    unscheduledOps.forEach(op => {
      const job = state.jobs.find(j => j.id === op.jobId) || {};
      const mach = state.machines.find(m => m.id === op.machineId) || {};
      const card = document.createElement('div');
      card.className = 'unscheduled-card';
      card.draggable = true;
      card.dataset.opId = op.id;

      const header = document.createElement('div');
      header.className = 'unscheduled-card-header';

      const badge = document.createElement('span');
      badge.className = 'op-id-badge';
      badge.style.backgroundColor = engine.sanitizeColor(job.color);
      badge.style.color = '#fff';
      badge.textContent = `${op.jobId} #${op.seq}`;
      header.appendChild(badge);

      const reqHours = document.createElement('span');
      reqHours.style.fontSize = '11px';
      reqHours.style.fontWeight = '700';
      reqHours.style.color = 'var(--accent-amber)';
      reqHours.textContent = `${op.totalHours}h required`;
      header.appendChild(reqHours);
      card.appendChild(header);

      const nameDiv = document.createElement('div');
      nameDiv.style.fontWeight = '600';
      nameDiv.style.color = '#fff';
      nameDiv.style.fontSize = '13px';
      nameDiv.textContent = op.name;
      card.appendChild(nameDiv);

      const metaDiv = document.createElement('div');
      metaDiv.style.fontSize = '11px';
      metaDiv.style.color = 'var(--text-muted)';
      metaDiv.textContent = `Target: ${mach.shortName || op.machineId} · Customer: ${job.customer || ''}`;
      card.appendChild(metaDiv);

      const footer = document.createElement('div');
      footer.style.display = 'flex';
      footer.style.justifyContent = 'space-between';
      footer.style.alignItems = 'center';
      footer.style.marginTop = '4px';

      const dueSpan = document.createElement('span');
      dueSpan.style.fontSize = '10px';
      dueSpan.style.color = 'var(--text-secondary)';
      dueSpan.textContent = `Due: ${engine.slotToTimeEnd(job.dueDateSlot).displayStr}`;
      footer.appendChild(dueSpan);

      const placeBtn = document.createElement('button');
      placeBtn.className = 'btn btn-outline';
      placeBtn.style.padding = '2px 8px';
      placeBtn.style.fontSize = '11px';
      placeBtn.dataset.opId = op.id;
      placeBtn.textContent = 'Place Now';
      placeBtn.addEventListener('click', () => {
        pushState(`Auto-schedule ${op.id}`);
        const prop = engine.proposeSchedule(state);
        state = prop.newState;
        selectOperation(op.id);
        renderAll();
      });
      footer.appendChild(placeBtn);
      card.appendChild(footer);

      card.addEventListener('dragstart', handleItemDragStart);
      card.addEventListener('dragend', handleItemDragEnd);

      list.appendChild(card);
    });

    container.querySelector('#btn-auto-place-all').addEventListener('click', () => {
      pushState('Auto-schedule all unscheduled work');
      const prop = engine.proposeSchedule(state);
      state = prop.newState;
      showNotification(prop.summaryText, false);
      renderAll();
    });
  }

  // --- Tab 3: Conflicts & Explanations Tab ---
  function renderConflictsTab(container) {
    const conflicts = engine.validateSchedule(state);

    if (conflicts.length === 0) {
      container.innerHTML = `
        <div style="text-align: center; padding: 40px 20px; color: var(--text-muted);">
          <div style="font-size: 36px; margin-bottom: 8px;">✨</div>
          <p style="font-weight: 600; color: var(--accent-emerald);">Zero Conflicts Detected</p>
          <p style="font-size: 12px; margin-top: 4px;">Machine capacity limits, planned outages, precedence constraints, and customer deadlines are fully satisfied.</p>
        </div>
      `;
      return;
    }

    container.innerHTML = `
      <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px;">
        <span style="font-size: 12px; font-weight: 700; color: #fca5a5;">${conflicts.length} Constraint Conflict(s)</span>
        <button class="btn btn-primary" id="btn-resolve-all-conflicts">⚡ Auto-Resolve All</button>
      </div>
      <div class="conflict-list"></div>
    `;

    const list = container.querySelector('.conflict-list');
    conflicts.forEach(c => {
      const card = document.createElement('div');
      card.className = 'conflict-card' + (c.severity === 'warning' ? ' warning' : '');

      const titleDiv = document.createElement('div');
      titleDiv.className = 'conflict-card-title';
      const iconSpan = document.createElement('span');
      iconSpan.textContent = c.severity === 'critical' ? '🚫 ' : '⚠️ ';
      const textSpan = document.createElement('span');
      textSpan.textContent = c.title;
      titleDiv.appendChild(iconSpan);
      titleDiv.appendChild(textSpan);
      card.appendChild(titleDiv);

      const descDiv = document.createElement('div');
      descDiv.className = 'conflict-desc';
      descDiv.textContent = c.description;
      card.appendChild(descDiv);

      if (c.suggestedAction) {
        const actionDiv = document.createElement('div');
        actionDiv.className = 'conflict-action-text';
        const iconSpanAction = document.createElement('span');
        iconSpanAction.textContent = '💡 ';
        const strong = document.createElement('strong');
        strong.textContent = 'Suggested Fix: ';
        const fixText = document.createTextNode(c.suggestedAction);
        actionDiv.appendChild(iconSpanAction);
        actionDiv.appendChild(strong);
        actionDiv.appendChild(fixText);
        card.appendChild(actionDiv);
      }

      if (c.operationId) {
        const btnRow = document.createElement('div');
        btnRow.style.display = 'flex';
        btnRow.style.justifyContent = 'flex-end';
        btnRow.style.marginTop = '4px';

        const inspectBtn = document.createElement('button');
        inspectBtn.className = 'btn btn-outline';
        inspectBtn.style.padding = '2px 8px';
        inspectBtn.style.fontSize = '11px';
        inspectBtn.dataset.opId = c.operationId;
        inspectBtn.textContent = 'Inspect Operation';
        inspectBtn.addEventListener('click', () => {
          selectOperation(c.operationId);
        });
        btnRow.appendChild(inspectBtn);
        card.appendChild(btnRow);
      }

      list.appendChild(card);
    });

    container.querySelector('#btn-resolve-all-conflicts').addEventListener('click', () => {
      pushState('Auto-resolve all schedule conflicts');
      const prop = engine.autoResolveConflicts(state);
      state = prop.newState;
      showNotification(prop.summaryText, false);
      renderAll();
    });
  }

  // --- Tab 4: Workshop Jobs Directory ---
  function renderJobsTab(container) {
    container.innerHTML = `
      <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px;">
        <span style="font-size: 12px; font-weight: 700; color: var(--text-secondary);">${state.jobs.length} Active Customer Jobs</span>
        <button class="btn btn-outline" id="btn-open-add-job">＋ Add Job</button>
      </div>
      <div style="display: flex; flex-direction: column; gap: 8px;" id="jobs-list"></div>
    `;

    const list = container.querySelector('#jobs-list');
    state.jobs.forEach(job => {
      const jobOps = state.operations.filter(o => o.jobId === job.id);
      const isComplete = jobOps.length > 0 && !jobOps.some(o => o.isUnscheduled);
      const lastOp = isComplete ? jobOps[jobOps.length - 1] : null;
      const isLate = lastOp && lastOp.endSlot > job.dueDateSlot;

      const card = document.createElement('div');
      card.className = 'inspector-card';

      const header = document.createElement('div');
      header.style.display = 'flex';
      header.style.alignItems = 'center';
      header.style.justifyContent = 'space-between';

      const idBadge = document.createElement('span');
      idBadge.className = 'op-id-badge';
      idBadge.style.backgroundColor = engine.sanitizeColor(job.color);
      idBadge.style.color = '#fff';
      idBadge.style.fontSize = '11px';
      idBadge.textContent = job.id;
      header.appendChild(idBadge);

      const statusBadge = document.createElement('span');
      statusBadge.className = 'op-status-badge';
      if (!isComplete) {
        statusBadge.style.backgroundColor = 'var(--accent-rose)';
        statusBadge.textContent = 'UNSCHEDULED';
      } else if (isLate) {
        statusBadge.style.backgroundColor = 'var(--accent-amber)';
        statusBadge.textContent = `LATE (+${((lastOp.endSlot - job.dueDateSlot)*0.5).toFixed(1)}h)`;
      } else {
        statusBadge.style.backgroundColor = 'var(--accent-emerald)';
        statusBadge.textContent = 'ON TIME';
      }
      header.appendChild(statusBadge);
      card.appendChild(header);

      const partDiv = document.createElement('div');
      partDiv.style.fontWeight = '700';
      partDiv.style.fontSize = '13px';
      partDiv.style.color = '#fff';
      partDiv.textContent = job.partName;
      card.appendChild(partDiv);

      const metaDiv = document.createElement('div');
      metaDiv.style.fontSize = '11px';
      metaDiv.style.color = 'var(--text-secondary)';
      const custLabel = document.createTextNode('Customer: ');
      const custStrong = document.createElement('strong');
      custStrong.textContent = job.customer;
      const qtyText = document.createTextNode(` · Qty: ${job.quantity} pcs`);
      metaDiv.appendChild(custLabel);
      metaDiv.appendChild(custStrong);
      metaDiv.appendChild(qtyText);
      card.appendChild(metaDiv);

      const footer = document.createElement('div');
      footer.style.display = 'flex';
      footer.style.alignItems = 'center';
      footer.style.justifyContent = 'space-between';
      footer.style.marginTop = '4px';

      const pGroup = document.createElement('div');
      pGroup.style.display = 'flex';
      pGroup.style.alignItems = 'center';
      pGroup.style.gap = '6px';
      const pLabel = document.createElement('label');
      pLabel.style.fontSize = '11px';
      pLabel.style.color = 'var(--text-muted)';
      pLabel.textContent = 'Priority:';
      pGroup.appendChild(pLabel);

      const pSelect = document.createElement('select');
      pSelect.className = 'form-select job-priority-select';
      pSelect.dataset.jobId = job.id;
      pSelect.style.padding = '2px 6px';
      pSelect.style.fontSize = '11px';
      Object.keys(engine.PRIORITIES).forEach(p => {
        const opt = document.createElement('option');
        opt.value = p;
        opt.textContent = `${engine.PRIORITIES[p].icon} ${engine.PRIORITIES[p].label}`;
        if (p === job.priority) opt.selected = true;
        pSelect.appendChild(opt);
      });
      pSelect.addEventListener('change', (e) => {
        const newPriority = e.target.value;
        pushState(`Change Priority of ${job.id} to ${newPriority}`);
        job.priority = newPriority;
        renderAll();
        showNotification(`Updated Priority of ${job.id} to ${newPriority}. Click Propose Schedule to re-order board.`, false);
      });
      pGroup.appendChild(pSelect);
      footer.appendChild(pGroup);

      const dueSpan = document.createElement('span');
      dueSpan.style.fontSize = '11px';
      dueSpan.style.color = 'var(--text-secondary)';
      dueSpan.textContent = `Due: ${engine.slotToTimeEnd(job.dueDateSlot).displayStr}`;
      footer.appendChild(dueSpan);
      card.appendChild(footer);

      list.appendChild(card);
    });

    container.querySelector('#btn-open-add-job').addEventListener('click', () => {
      openModal(DOM.modalAddJob);
    });
  }

  // --- Tab 5: Machines & Outages Tab ---
  function renderMachinesTab(container) {
    const kpis = engine.calculateKPIs(state);

    container.innerHTML = `
      <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px;">
        <span style="font-size: 12px; font-weight: 700; color: var(--text-secondary);">Machines & Maintenance</span>
        <button class="btn btn-outline" id="btn-open-add-outage">＋ Add Outage</button>
      </div>
      <div style="display: flex; flex-direction: column; gap: 10px;" id="machines-list"></div>
    `;

    const list = container.querySelector('#machines-list');
    state.machines.forEach(machine => {
      const machKpi = kpis.machineKPIs[machine.id] || { utilizationPercent: 0 };
      const outages = state.outages.filter(o => o.machineId === machine.id);

      const card = document.createElement('div');
      card.className = 'inspector-card';

      const header = document.createElement('div');
      header.style.display = 'flex';
      header.style.alignItems = 'center';
      header.style.justifyContent = 'space-between';

      const nameH4 = document.createElement('h4');
      nameH4.style.color = '#fff';
      nameH4.style.fontSize = '13px';
      nameH4.textContent = machine.name;
      header.appendChild(nameH4);

      const codeBadge = document.createElement('span');
      codeBadge.className = 'machine-badge';
      codeBadge.textContent = machine.code;
      header.appendChild(codeBadge);
      card.appendChild(header);

      const rateDiv = document.createElement('div');
      rateDiv.style.fontSize = '11px';
      rateDiv.style.color = 'var(--text-secondary)';
      rateDiv.textContent = `${machine.workCenter} · $${machine.hourlyRate}/hr`;
      card.appendChild(rateDiv);

      const descDiv = document.createElement('div');
      descDiv.style.fontSize = '11px';
      descDiv.style.color = 'var(--text-muted)';
      descDiv.textContent = machine.description;
      card.appendChild(descDiv);

      const utilRow = document.createElement('div');
      utilRow.style.marginTop = '6px';
      utilRow.style.display = 'flex';
      utilRow.style.alignItems = 'center';
      utilRow.style.justifyContent = 'space-between';
      const utilLbl = document.createElement('span');
      utilLbl.style.fontSize = '11px';
      utilLbl.style.color = 'var(--text-secondary)';
      utilLbl.textContent = 'Utilization:';
      const utilVal = document.createElement('span');
      utilVal.style.fontSize = '12px';
      utilVal.style.fontWeight = '700';
      utilVal.style.color = 'var(--accent-emerald)';
      utilVal.textContent = `${machKpi.utilizationPercent}%`;
      utilRow.appendChild(utilLbl);
      utilRow.appendChild(utilVal);
      card.appendChild(utilRow);

      if (outages.length > 0) {
        const outSection = document.createElement('div');
        outSection.style.marginTop = '8px';
        outSection.style.borderTop = '1px solid var(--border-color)';
        outSection.style.paddingTop = '6px';

        const outTitle = document.createElement('div');
        outTitle.style.fontSize = '11px';
        outTitle.style.fontWeight = '700';
        outTitle.style.color = '#fca5a5';
        outTitle.style.marginBottom = '4px';
        outTitle.textContent = 'Planned Outages:';
        outSection.appendChild(outTitle);

        outages.forEach(o => {
          const row = document.createElement('div');
          row.style.fontSize = '11px';
          row.style.backgroundColor = 'rgba(239, 68, 68, 0.1)';
          row.style.padding = '4px 6px';
          row.style.borderRadius = '4px';
          row.style.display = 'flex';
          row.style.justifyContent = 'space-between';
          row.style.alignItems = 'center';
          row.style.marginBottom = '4px';

          const textSpan = document.createElement('span');
          textSpan.textContent = `${o.title} (${engine.formatSlotRange(o.startSlot, o.endSlot)})`;
          row.appendChild(textSpan);

          const rmBtn = document.createElement('button');
          rmBtn.className = 'btn btn-outline';
          rmBtn.style.padding = '1px 4px';
          rmBtn.style.fontSize = '10px';
          rmBtn.dataset.outageId = o.id;
          rmBtn.textContent = 'Remove';
          rmBtn.addEventListener('click', () => {
            pushState(`Remove outage ${o.id}`);
            state.outages = state.outages.filter(out => out.id !== o.id);
            renderAll();
            showNotification(`Removed outage from ${machine.shortName}.`, false);
          });
          row.appendChild(rmBtn);
          outSection.appendChild(row);
        });
        card.appendChild(outSection);
      } else {
        const noOutDiv = document.createElement('div');
        noOutDiv.style.fontSize = '11px';
        noOutDiv.style.color = 'var(--text-muted)';
        noOutDiv.style.marginTop = '4px';
        noOutDiv.textContent = 'No planned outages this week.';
        card.appendChild(noOutDiv);
      }

      list.appendChild(card);
    });

    container.querySelector('#btn-open-add-outage').addEventListener('click', () => {
      openModal(DOM.modalAddOutage);
    });
  }

  // --- Modal Helpers ---
  function openModal(modal) {
    if (!modal) return;
    modal.classList.add('open');
  }

  function closeModal(modal) {
    if (!modal) return;
    modal.classList.remove('open');
  }

  function setupModals() {
    // Close on backdrop click or close button
    document.querySelectorAll('.modal-overlay').forEach(modal => {
      modal.addEventListener('click', (e) => {
        if (e.target === modal || e.target.classList.contains('btn-close-modal')) {
          closeModal(modal);
        }
      });
    });

    // KPI Info Modal
    DOM.btnKpiInfo.addEventListener('click', () => {
      const kpis = engine.calculateKPIs(state);
      const body = DOM.modalKpi.querySelector('.modal-body');
      body.innerHTML = `
        <h3>Machine Utilization Formula</h3>
        <p><strong>Equation:</strong> <code>Utilization (%) = (Total Scheduled Hours / Available Operating Hours) × 100%</code></p>
        <p>Where <em>Available Operating Hours</em> = 45.0h (5 days × 9h) minus planned machine outages.</p>
        <p>Total Scheduled Hours includes both setup time and run time for all confirmed operations on that machine.</p>

        <h3>Workshop Machine Breakdown (Live Numbers)</h3>
        <table style="width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 12px;">
          <thead>
            <tr style="border-bottom: 1px solid var(--border-light); text-align: left;">
              <th style="padding: 6px;">Machine</th>
              <th style="padding: 6px;">Available</th>
              <th style="padding: 6px;">Outage</th>
              <th style="padding: 6px;">Setup</th>
              <th style="padding: 6px;">Run</th>
              <th style="padding: 6px;">Utilization</th>
            </tr>
          </thead>
          <tbody id="kpi-modal-tbody"></tbody>
        </table>

        <h3>On-Time Delivery & Tardiness</h3>
        <p><strong>Planned Completion:</strong> Determined by the conclusion of the final operation in each job's ordered sequence.</p>
        <p><strong>Lateness Condition:</strong> If <code>Final Op End Time > Customer Due Date</code>, the job is marked <strong>LATE</strong>.</p>
        <p id="kpi-modal-tardiness-desc"></p>
      `;

      const tbody = body.querySelector('#kpi-modal-tbody');
      state.machines.forEach(m => {
        const k = kpis.machineKPIs[m.id] || { availableHours: 45, outageHours: 0, setupHours: 0, runHours: 0, utilizationPercent: 0 };
        const tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid var(--border-color)';

        const tdName = document.createElement('td');
        tdName.style.padding = '6px';
        const strong = document.createElement('strong');
        strong.textContent = m.shortName;
        tdName.appendChild(strong);
        tr.appendChild(tdName);

        const tdAvail = document.createElement('td');
        tdAvail.style.padding = '6px';
        tdAvail.textContent = `${k.availableHours}h`;
        tr.appendChild(tdAvail);

        const tdOutage = document.createElement('td');
        tdOutage.style.padding = '6px';
        tdOutage.textContent = `${k.outageHours}h`;
        tr.appendChild(tdOutage);

        const tdSetup = document.createElement('td');
        tdSetup.style.padding = '6px';
        tdSetup.textContent = `${k.setupHours}h`;
        tr.appendChild(tdSetup);

        const tdRun = document.createElement('td');
        tdRun.style.padding = '6px';
        tdRun.textContent = `${k.runHours}h`;
        tr.appendChild(tdRun);

        const tdUtil = document.createElement('td');
        tdUtil.style.padding = '6px';
        tdUtil.style.fontWeight = '700';
        tdUtil.style.color = 'var(--accent-cyan)';
        tdUtil.textContent = `${k.utilizationPercent}%`;
        tr.appendChild(tdUtil);

        tbody.appendChild(tr);
      });

      const tardinessP = body.querySelector('#kpi-modal-tardiness-desc');
      tardinessP.textContent = `(Finish Slot - Due Date Slot) × 0.5 hours. Currently: ${kpis.lateJobsCount} late job(s) totaling ${kpis.totalTardinessHours}h.`;

      openModal(DOM.modalKpi);
    });

    // Export Modal
    DOM.btnExport.addEventListener('click', () => {
      const md = engine.generateMarkdownExport(state);
      const csv = engine.generateCSVExport(state);

      const mdPreview = DOM.modalExport.querySelector('#export-md-preview');
      const csvPreview = DOM.modalExport.querySelector('#export-csv-preview');
      if (mdPreview) mdPreview.textContent = md;
      if (csvPreview) csvPreview.textContent = csv;

      openModal(DOM.modalExport);
    });

    // Help & Walkthrough Modal
    DOM.btnHelp.addEventListener('click', () => {
      openModal(DOM.modalHelp);
    });

    // Export Download & Copy Handlers
    const btnCopyMd = DOM.modalExport.querySelector('#btn-copy-md');
    if (btnCopyMd) {
      btnCopyMd.addEventListener('click', () => {
        const md = engine.generateMarkdownExport(state);
        navigator.clipboard.writeText(md).then(() => {
          btnCopyMd.textContent = 'Copied!';
          setTimeout(() => { btnCopyMd.textContent = 'Copy Markdown'; }, 2000);
        });
      });
    }

    const btnDownloadMd = DOM.modalExport.querySelector('#btn-download-md');
    if (btnDownloadMd) {
      btnDownloadMd.addEventListener('click', () => {
        const md = engine.generateMarkdownExport(state);
        downloadFile('shiftboard-dispatch-report.md', md, 'text/markdown');
      });
    }

    const btnCopyCsv = DOM.modalExport.querySelector('#btn-copy-csv');
    if (btnCopyCsv) {
      btnCopyCsv.addEventListener('click', () => {
        const csv = engine.generateCSVExport(state);
        navigator.clipboard.writeText(csv).then(() => {
          btnCopyCsv.textContent = 'Copied!';
          setTimeout(() => { btnCopyCsv.textContent = 'Copy CSV'; }, 2000);
        });
      });
    }

    const btnDownloadCsv = DOM.modalExport.querySelector('#btn-download-csv');
    if (btnDownloadCsv) {
      btnDownloadCsv.addEventListener('click', () => {
        const csv = engine.generateCSVExport(state);
        downloadFile('shiftboard-operations.csv', csv, 'text/csv');
      });
    }

    // Add Job Form Submit
    const formAddJob = document.getElementById('form-add-job');
    if (formAddJob) {
      formAddJob.addEventListener('submit', (e) => {
        e.preventDefault();
        const id = document.getElementById('input-job-id').value.trim();
        const customer = document.getElementById('input-job-customer').value.trim();
        const partName = document.getElementById('input-job-part').value.trim();
        const quantity = parseInt(document.getElementById('input-job-qty').value, 10) || 10;
        const priority = document.getElementById('input-job-priority').value;
        const dayIdx = parseInt(document.getElementById('input-job-due-day').value, 10);
        const hour = parseInt(document.getElementById('input-job-due-hour').value, 10);
        const dueDateSlot = engine.timeToSlot(dayIdx, hour);

        // Check unique ID
        if (state.jobs.some(j => j.id === id)) {
          alert(`Job ID ${id} already exists.`);
          return;
        }

        const colors = ['#2563eb', '#dc2626', '#059669', '#7c3aed', '#ea580c', '#0891b2', '#4f46e5', '#d97706'];
        const color = colors[state.jobs.length % colors.length];

        const newJob = { id, customer, partName, quantity, dueDateSlot, priority, color };

        // Add 2 default operations for this job
        const op1 = {
          id: `OP-${id}-1`,
          jobId: id,
          seq: 10,
          name: `${partName} - Blanking`,
          machineId: 'M4',
          setupHours: 0.5,
          runHours: 2.0,
          totalHours: 2.5,
          totalSlots: 5,
          setupSlots: 1,
          runSlots: 4,
          isUnscheduled: true,
          startSlot: null,
          endSlot: null
        };
        const op2 = {
          id: `OP-${id}-2`,
          jobId: id,
          seq: 20,
          name: `${partName} - Machining`,
          machineId: 'M2',
          setupHours: 1.0,
          runHours: 3.0,
          totalHours: 4.0,
          totalSlots: 8,
          setupSlots: 2,
          runSlots: 6,
          isUnscheduled: true,
          startSlot: null,
          endSlot: null
        };

        pushState(`Added new job ${id}`);
        state.jobs.push(newJob);
        state.operations.push(op1, op2);

        closeModal(DOM.modalAddJob);
        activeTab = 'unscheduled';
        renderAll();
        showNotification(`Added Job ${id} with 2 operations in the unscheduled queue. Click Auto-Schedule to place.`, false);
      });
    }

    // Add Outage Form Submit
    const formAddOutage = document.getElementById('form-add-outage');
    if (formAddOutage) {
      formAddOutage.addEventListener('submit', (e) => {
        e.preventDefault();
        const machineId = document.getElementById('input-outage-machine').value;
        const title = document.getElementById('input-outage-title').value.trim();
        const reason = document.getElementById('input-outage-reason').value.trim();
        const dayIdx = parseInt(document.getElementById('input-outage-day').value, 10);
        const startH = parseInt(document.getElementById('input-outage-start').value, 10);
        const endH = parseInt(document.getElementById('input-outage-end').value, 10);

        if (endH <= startH) {
          alert('End hour must be after start hour.');
          return;
        }

        const startSlot = engine.timeToSlot(dayIdx, startH);
        const endSlot = engine.timeToSlot(dayIdx, endH - 1, 30); // inclusive end slot

        const newOutage = {
          id: `OUT-${Date.now()}`,
          machineId,
          title,
          reason,
          dayIndex: dayIdx,
          startSlot,
          endSlot,
          type: 'maintenance'
        };

        pushState(`Added Outage on ${machineId}`);
        state.outages.push(newOutage);

        closeModal(DOM.modalAddOutage);
        renderAll();
        showNotification(`Planned outage added on ${machineId} (${engine.formatSlotRange(startSlot, endSlot)}).`, false);
      });
    }
  }

  function downloadFile(filename, content, mimeType) {
    const blob = new Blob([content], { type: mimeType });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  // --- Actions & Buttons Handlers ---
  function setupActions() {
    // Auto-Schedule (Propose Schedule)
    DOM.btnPropose.addEventListener('click', () => {
      pushState('Auto-Schedule proposal');
      const result = engine.proposeSchedule(state);
      state = result.newState;
      renderAll();

      if (result.changes.length > 0) {
        showNotification(`⚡ Auto-Scheduled: ${result.summaryText}`, false);
      } else {
        showNotification('Schedule is already optimal according to job priorities and due dates.', false);
      }
    });

    // Undo / Redo
    DOM.btnUndo.addEventListener('click', handleUndo);
    DOM.btnRedo.addEventListener('click', handleRedo);

    // Keyboard Shortcuts (Ctrl+Z / Ctrl+Y)
    window.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
        e.preventDefault();
        if (e.shiftKey) {
          handleRedo();
        } else {
          handleUndo();
        }
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') {
        e.preventDefault();
        handleRedo();
      } else if (e.key === 'Escape') {
        document.querySelectorAll('.modal-overlay.open').forEach(closeModal);
      }
    });

    // Scenario 1: Simulate Urgent Order
    DOM.btnScenarioUrgent.addEventListener('click', () => {
      pushState('Simulate Urgent Order');
      const res = engine.applyUrgentOrderScenario(state);
      if (!res.applied) {
        showNotification(res.message, true);
        return;
      }
      state = res.newState;
      selectOperation('OP-901-1');
      renderAll();
      showNotification(res.message, true, () => {
        pushState('Auto-Resolve Urgent Order Conflicts');
        const prop = engine.proposeSchedule(state);
        state = prop.newState;
        renderAll();
        showNotification(`⚡ Auto-Scheduled: ${prop.summaryText}`, false);
      }, 'Auto-Resolve Conflicts', '⚡');
    });

    // Scenario 2: Simulate Extended Outage
    DOM.btnScenarioOutage.addEventListener('click', () => {
      pushState('Simulate Extended Outage');
      const res = engine.applyExtendedOutageScenario(state);
      state = res.newState;
      renderAll();
      showNotification(res.message, true, () => {
        pushState('Auto-Resolve Extended Outage Conflicts');
        const prop = engine.autoResolveConflicts(state);
        state = prop.newState;
        renderAll();
        showNotification(`⚡ Auto-Scheduled: ${prop.summaryText}`, false);
      }, 'Auto-Resolve Conflicts', '⚡');
    });

    // Restore Original Demo
    DOM.btnRestoreDemo.addEventListener('click', () => {
      pushState('Restore Original Demo State');
      state = engine.cloneState(baselineState);
      selectedOpId = null;
      renderAll();
      showNotification('Restored workshop schedule to pristine initial demo state.', false);
    });

    // Save Plan (Download JSON & LocalStorage)
    DOM.btnSavePlan.addEventListener('click', () => {
      const dataStr = JSON.stringify(state, null, 2);
      try {
        localStorage.setItem('shiftboard_active_plan', dataStr);
      } catch (err) {
        console.warn('LocalStorage save failed:', err);
      }
      const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
      downloadFile(`shiftboard-plan-${timestamp}.json`, dataStr, 'application/json');
      showNotification('Plan saved to local file and browser storage snapshot.', false);
    });

    // Load Plan (File Upload)
    DOM.btnLoadPlan.addEventListener('click', () => {
      DOM.fileInput.click();
    });

    DOM.fileInput.addEventListener('change', (e) => {
      const file = e.target.files[0];
      if (!file) return;

      // File size limit: 5MB max
      if (file.size > 5 * 1024 * 1024) {
        alert('File size exceeds maximum allowed limit (5MB).');
        DOM.fileInput.value = '';
        return;
      }

      const reader = new FileReader();
      reader.onload = (event) => {
        try {
          let parsedData;
          try {
            parsedData = JSON.parse(event.target.result);
          } catch (jsonErr) {
            throw new Error('File is not valid JSON: ' + jsonErr.message);
          }

          const validation = engine.validatePlanSchema(parsedData);
          if (!validation.valid) {
            throw new Error('Plan schema validation failed:\n• ' + validation.errors.join('\n• '));
          }

          const loadedState = validation.sanitizedState;
          pushState(`Loaded plan from ${file.name}`);
          state = loadedState;
          selectedOpId = null;
          renderAll();
          showNotification(`Successfully loaded and validated plan from ${file.name}`, false);
        } catch (err) {
          alert('Failed to load plan: ' + err.message);
        }
      };
      reader.readAsText(file);
      DOM.fileInput.value = '';
    });

    // Drawer Tabs Toggle
    DOM.drawerTabs.forEach(tabBtn => {
      tabBtn.addEventListener('click', () => {
        activeTab = tabBtn.dataset.tab;
        renderDrawerTabs();
        renderDrawerContent();
      });
    });

    // Drawer Toggle / Collapse
    DOM.btnToggleDrawer.addEventListener('click', () => {
      isDrawerOpen = !isDrawerOpen;
      DOM.sideDrawer.classList.toggle('collapsed', !isDrawerOpen);
    });
    DOM.btnCloseDrawer.addEventListener('click', () => {
      isDrawerOpen = false;
      DOM.sideDrawer.classList.add('collapsed');
    });

    // Dismiss Notification
    DOM.btnNotificationDismiss.addEventListener('click', hideNotification);
  }

  // --- Comprehensive Render ---
  function renderAll() {
    renderScheduleBoard();
    updateKPIRibbon();
    renderDrawerTabs();
    renderDrawerContent();
  }

  // --- Initialization ---
  function init() {
    setupModals();
    setupActions();
    updateUndoRedoButtons();
    renderAll();
    console.log('ShiftBoard initialized successfully.');
  }

  // Run on DOM load
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
