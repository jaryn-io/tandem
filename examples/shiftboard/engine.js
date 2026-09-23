/**
 * ShiftBoard Scheduling Engine
 * Core domain models, working calendar, conflict detection,
 * heuristic forward-scheduling, KPI analytics, and export generation.
 *
 * Universal Module Definition (Node.js & Browser compatible)
 */

(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.ShiftBoardEngine = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // --- Constants & Calendar Definitions ---
  const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'];
  const DAY_LABELS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'];
  const WORK_START_HOUR = 8; // 08:00
  const WORK_END_HOUR = 17;  // 17:00
  const HOURS_PER_DAY = WORK_END_HOUR - WORK_START_HOUR; // 9 hours
  const SLOTS_PER_HOUR = 2; // 30-minute intervals
  const SLOTS_PER_DAY = HOURS_PER_DAY * SLOTS_PER_HOUR; // 18 slots (9 hours * 2)
  const TOTAL_DAYS = 5;
  const TOTAL_SLOTS = TOTAL_DAYS * SLOTS_PER_DAY; // 90 slots across the week (45 working hours)
  const SLOT_DURATION_HOURS = 0.5; // each slot is 30 minutes

  // Priority metadata
  const PRIORITIES = {
    URGENT: { key: 'URGENT', label: 'Urgent', rank: 4, icon: '⚡', badgeClass: 'priority-urgent' },
    HIGH:   { key: 'HIGH',   label: 'High',   rank: 3, icon: '▲', badgeClass: 'priority-high' },
    NORMAL: { key: 'NORMAL', label: 'Normal', rank: 2, icon: '●', badgeClass: 'priority-normal' },
    LOW:    { key: 'LOW',    label: 'Low',    rank: 1, icon: '▼', badgeClass: 'priority-low' }
  };

  // --- Time & Slot Helpers ---
  function slotToTime(slot) {
    if (slot < 0) slot = 0;
    if (slot >= TOTAL_SLOTS) slot = TOTAL_SLOTS - 1;
    const dayIndex = Math.floor(slot / SLOTS_PER_DAY);
    const daySlot = slot % SLOTS_PER_DAY;
    const hour = WORK_START_HOUR + Math.floor(daySlot / SLOTS_PER_HOUR);
    const minute = (daySlot % SLOTS_PER_HOUR) * 30;
    return {
      slot,
      dayIndex,
      dayName: DAYS[dayIndex],
      dayLabel: DAY_LABELS[dayIndex],
      hour,
      minute,
      timeStr: String(hour).padStart(2, '0') + ':' + String(minute).padStart(2, '0'),
      displayStr: DAYS[dayIndex] + ' ' + String(hour).padStart(2, '0') + ':' + String(minute).padStart(2, '0')
    };
  }

  function slotToTimeEnd(slot) {
    if (slot === null || slot === undefined) return { displayStr: 'N/A', timeStr: 'N/A', dayName: 'N/A' };
    if (slot < 0) slot = 0;
    const dayIndex = Math.floor(slot / SLOTS_PER_DAY);
    const daySlot = slot % SLOTS_PER_DAY;
    let nextDaySlot = daySlot + 1;
    let nextDayIndex = dayIndex;
    let hour, minute;

    if (nextDaySlot >= SLOTS_PER_DAY) {
      hour = WORK_END_HOUR;
      minute = 0;
    } else {
      hour = WORK_START_HOUR + Math.floor(nextDaySlot / SLOTS_PER_HOUR);
      minute = (nextDaySlot % SLOTS_PER_HOUR) * 30;
    }

    const safeDayIndex = Math.min(nextDayIndex, TOTAL_DAYS - 1);
    const isPastHorizon = nextDayIndex >= TOTAL_DAYS;
    const dayName = DAYS[safeDayIndex] || 'Fri';
    const dayLabel = DAY_LABELS[safeDayIndex] || 'Friday';
    const horizonSuffix = isPastHorizon ? ` (+${((slot - TOTAL_SLOTS + 1) * SLOT_DURATION_HOURS).toFixed(1)}h past horizon)` : '';

    return {
      slot,
      dayIndex: nextDayIndex,
      dayName: isPastHorizon ? `${dayName} [Over Horizon]` : dayName,
      dayLabel: isPastHorizon ? `${dayLabel} [Over Horizon]` : dayLabel,
      hour,
      minute,
      timeStr: String(hour).padStart(2, '0') + ':' + String(minute).padStart(2, '0'),
      displayStr: DAYS[safeDayIndex] + ' ' + String(hour).padStart(2, '0') + ':' + String(minute).padStart(2, '0') + horizonSuffix
    };
  }

  function timeToSlot(dayIndex, hour, minute) {
    minute = minute || 0;
    if (dayIndex < 0) dayIndex = 0;
    if (dayIndex >= TOTAL_DAYS) dayIndex = TOTAL_DAYS - 1;
    if (hour >= WORK_END_HOUR) {
      // 17:00 represents the shift end of the specified day (last 30-min slot of that day)
      return dayIndex * SLOTS_PER_DAY + SLOTS_PER_DAY - 1;
    }
    const clampedHour = Math.max(hour, WORK_START_HOUR);
    const minSlot = minute >= 30 ? 1 : 0;
    return dayIndex * SLOTS_PER_DAY + (clampedHour - WORK_START_HOUR) * SLOTS_PER_HOUR + minSlot;
  }

  function formatSlotRange(startSlot, endSlot) {
    if (startSlot === null || startSlot === undefined || startSlot < 0) return 'Unscheduled';
    const s = slotToTime(startSlot);
    const e = slotToTimeEnd(endSlot);
    const durationHours = (endSlot - startSlot + 1) * SLOT_DURATION_HOURS;
    return s.displayStr + ' – ' + e.displayStr + ' (' + durationHours.toFixed(1) + 'h)';
  }

  // --- Initial Default Workshop State ---
  function getDefaultWorkshop() {
    const machines = [
      {
        id: 'M1',
        name: 'Haas UMC-750 (5-Axis CNC Mill)',
        shortName: '5-Axis Mill (M1)',
        code: 'UMC-750',
        workCenter: 'Precision 5-Axis Milling',
        hourlyRate: 145,
        color: '#2563eb',
        description: 'Simultaneous 5-axis machining center for aerospace and complex contours.'
      },
      {
        id: 'M2',
        name: 'Haas VF-2SS (3-Axis CNC Mill)',
        shortName: '3-Axis Mill (M2)',
        code: 'VF-2SS',
        workCenter: 'High-Speed Prismatic Milling',
        hourlyRate: 95,
        color: '#059669',
        description: 'High-speed 12,000 RPM vertical mill for bolt patterns, pockets, and finishing.'
      },
      {
        id: 'M3',
        name: 'Doosan Lynx 2100 (CNC Lathe)',
        shortName: 'CNC Lathe (M3)',
        code: 'LYNX-2100',
        workCenter: 'Turning & Threading',
        hourlyRate: 85,
        color: '#7c3aed',
        description: 'Precision turning center for cylindrical shafts, threading, and boring.'
      },
      {
        id: 'M4',
        name: 'Amada Ensis 3015 (Fiber Laser)',
        shortName: 'Fiber Laser (M4)',
        code: 'ENSIS-3015',
        workCenter: 'Sheet Cutting & Blanking',
        hourlyRate: 120,
        color: '#d97706',
        description: '3kW fiber laser cutting plate stock, profiles, and prep blanking.'
      },
      {
        id: 'M5',
        name: 'Zeiss DuraMax (CMM Metrology)',
        shortName: 'CMM Metrology (M5)',
        code: 'DURAMAX',
        workCenter: 'Quality Assurance & CMM',
        hourlyRate: 75,
        color: '#db2777',
        description: 'Shop-floor coordinate measuring machine and surface roughness inspection.'
      }
    ];

    const outages = [
      {
        id: 'OUT-01',
        machineId: 'M1',
        title: 'Spindle Bearing Preventive Maintenance',
        reason: 'Scheduled bearing vibration check & lubrication flush',
        dayIndex: 1, // Tuesday
        startSlot: 28, // Tue 13:00
        endSlot: 35,   // Tue 16:30 (ends 17:00) -> 8 slots = 4.0 hours
        type: 'maintenance'
      }
    ];

    const jobs = [
      {
        id: 'JOB-101',
        customer: 'Apex Aerospace',
        partName: 'Titanium Impeller Bracket',
        quantity: 20,
        dueDateSlot: 50, // Wed 15:00
        priority: 'HIGH',
        color: '#2563eb'
      },
      {
        id: 'JOB-102',
        customer: 'BioVasc Medical',
        partName: '316L Vascular Valve Manifold',
        quantity: 50,
        dueDateSlot: 66, // Thu 14:00
        priority: 'URGENT',
        color: '#dc2626'
      },
      {
        id: 'JOB-103',
        customer: 'Nordic Hydraulics',
        partName: 'High-Pressure Pump Cylinder',
        quantity: 15,
        dueDateSlot: 44, // Wed 12:00
        priority: 'NORMAL',
        color: '#059669'
      },
      {
        id: 'JOB-104',
        customer: 'Turbine Dynamics',
        partName: 'Inconel Nozzle Guide Ring',
        quantity: 8,
        dueDateSlot: 78, // Fri 11:00
        priority: 'NORMAL',
        color: '#7c3aed'
      },
      {
        id: 'JOB-105',
        customer: 'Vanguard Defense',
        partName: 'Armor Mounting Pivot Pin',
        quantity: 60,
        dueDateSlot: 53, // Wed 17:00
        priority: 'HIGH',
        color: '#ea580c'
      },
      {
        id: 'JOB-106',
        customer: 'Kinetics Robotix',
        partName: 'Articulated Joint Housing',
        quantity: 30,
        dueDateSlot: 84, // Fri 14:00
        priority: 'NORMAL',
        color: '#0891b2'
      },
      {
        id: 'JOB-107',
        customer: 'Solaris Energy',
        partName: 'Concentrator Pivot Bracket',
        quantity: 120,
        dueDateSlot: 88, // Fri 16:00
        priority: 'LOW',
        color: '#4f46e5'
      },
      {
        id: 'JOB-108',
        customer: 'Precision Hydraulics',
        partName: 'Valve Spool Assembly',
        quantity: 40,
        dueDateSlot: 80, // Fri 12:00
        priority: 'NORMAL',
        color: '#d97706'
      }
    ];

    const operations = [
      // JOB-101: Apex Aerospace (High)
      {
        id: 'OP-101-1',
        jobId: 'JOB-101',
        seq: 10,
        name: 'Plate Blanking',
        machineId: 'M4',
        setupHours: 0.5,
        runHours: 1.5,
        startSlot: 0, // Mon 08:00
        endSlot: 3    // Mon 10:00 (4 slots = 2.0h)
      },
      {
        id: 'OP-101-2',
        jobId: 'JOB-101',
        seq: 20,
        name: '5-Axis Contouring',
        machineId: 'M1',
        setupHours: 1.0,
        runHours: 3.0,
        startSlot: 4, // Mon 10:00
        endSlot: 11   // Mon 14:00 (8 slots = 4.0h)
      },
      {
        id: 'OP-101-3',
        jobId: 'JOB-101',
        seq: 30,
        name: 'CMM Inspection',
        machineId: 'M5',
        setupHours: 0.5,
        runHours: 1.0,
        startSlot: 12, // Mon 14:00
        endSlot: 14    // Mon 15:30 (3 slots = 1.5h)
      },

      // JOB-102: BioVasc Medical (Urgent ⚡) - 4 Operations
      {
        id: 'OP-102-1',
        jobId: 'JOB-102',
        seq: 10,
        name: 'Bar Stock Cut',
        machineId: 'M4',
        setupHours: 0.5,
        runHours: 1.0,
        startSlot: 4, // Mon 10:00
        endSlot: 6    // Mon 11:30 (3 slots = 1.5h)
      },
      {
        id: 'OP-102-2',
        jobId: 'JOB-102',
        seq: 20,
        name: 'Turn & Core Bore',
        machineId: 'M3',
        setupHours: 1.0,
        runHours: 3.0,
        startSlot: 10, // Mon 13:00
        endSlot: 17    // Mon 17:00 (8 slots = 4.0h)
      },
      {
        id: 'OP-102-3',
        jobId: 'JOB-102',
        seq: 30,
        name: 'Micro-Port Milling',
        machineId: 'M2',
        setupHours: 1.0,
        runHours: 3.0,
        startSlot: 18, // Tue 08:00
        endSlot: 25    // Tue 12:00 (8 slots = 4.0h)
      },
      {
        id: 'OP-102-4',
        jobId: 'JOB-102',
        seq: 40,
        name: 'Cleanroom Metrology',
        machineId: 'M5',
        setupHours: 0.5,
        runHours: 1.5,
        startSlot: 26, // Tue 12:00
        endSlot: 29    // Tue 14:00 (4 slots = 2.0h)
      },

      // JOB-103: Nordic Hydraulics (Normal)
      {
        id: 'OP-103-1',
        jobId: 'JOB-103',
        seq: 10,
        name: 'Cylinder Turning',
        machineId: 'M3',
        setupHours: 1.0,
        runHours: 4.0,
        startSlot: 0, // Mon 08:00
        endSlot: 9    // Mon 13:00 (10 slots = 5.0h)
      },
      {
        id: 'OP-103-2',
        jobId: 'JOB-103',
        seq: 20,
        name: 'Bolt Pattern Milling',
        machineId: 'M2',
        setupHours: 0.5,
        runHours: 2.5,
        startSlot: 26, // Tue 12:00
        endSlot: 31    // Tue 15:00 (6 slots = 3.0h)
      },

      // JOB-104: Turbine Dynamics (Normal)
      {
        id: 'OP-104-1',
        jobId: 'JOB-104',
        seq: 10,
        name: 'Laser Blanking',
        machineId: 'M4',
        setupHours: 0.5,
        runHours: 2.0,
        startSlot: 7,  // Mon 11:30
        endSlot: 11   // Mon 14:00 (5 slots = 2.5h)
      },
      {
        id: 'OP-104-2',
        jobId: 'JOB-104',
        seq: 20,
        name: '5-Axis Airfoil Mill',
        machineId: 'M1',
        setupHours: 1.5,
        runHours: 4.5,
        startSlot: 12, // Mon 14:00 to Tue 11:00 (spans shift boundary, finishes before Tue PM outage)
        endSlot: 23
      },
      {
        id: 'OP-104-3',
        jobId: 'JOB-104',
        seq: 30,
        name: 'Optical Verification',
        machineId: 'M5',
        setupHours: 0.5,
        runHours: 1.5,
        startSlot: 30, // Tue 14:00
        endSlot: 33    // Tue 16:00 (4 slots = 2.0h)
      },

      // JOB-105: Vanguard Defense (High)
      {
        id: 'OP-105-1',
        jobId: 'JOB-105',
        seq: 10,
        name: 'Turn, Thread & Groove',
        machineId: 'M3',
        setupHours: 1.0,
        runHours: 3.5,
        startSlot: 18, // Tue 08:00
        endSlot: 26    // Tue 12:30 (9 slots = 4.5h)
      },
      {
        id: 'OP-105-2',
        jobId: 'JOB-105',
        seq: 20,
        name: 'Pin Flats Milling',
        machineId: 'M2',
        setupHours: 0.5,
        runHours: 1.5,
        startSlot: 32, // Tue 15:00
        endSlot: 35    // Tue 17:00 (4 slots = 2.0h)
      },

      // JOB-106: Kinetics Robotix (Normal)
      {
        id: 'OP-106-1',
        jobId: 'JOB-106',
        seq: 10,
        name: 'Pocket Milling',
        machineId: 'M2',
        setupHours: 1.0,
        runHours: 4.0,
        startSlot: 36, // Wed 08:00
        endSlot: 45    // Wed 13:00 (10 slots = 5.0h)
      },
      {
        id: 'OP-106-2',
        jobId: 'JOB-106',
        seq: 20,
        name: 'Bearing Bore Finishing',
        machineId: 'M1',
        setupHours: 1.0,
        runHours: 2.5,
        startSlot: 46, // Wed 13:00
        endSlot: 52    // Wed 16:30 (7 slots = 3.5h)
      },
      {
        id: 'OP-106-3',
        jobId: 'JOB-106',
        seq: 30,
        name: 'Bore Micrometry',
        machineId: 'M5',
        setupHours: 0.5,
        runHours: 1.0,
        startSlot: 54, // Thu 08:00
        endSlot: 56    // Thu 09:30 (3 slots = 1.5h)
      },

      // JOB-107: Solaris Energy (Low)
      {
        id: 'OP-107-1',
        jobId: 'JOB-107',
        seq: 10,
        name: 'Nesting & Laser Cut',
        machineId: 'M4',
        setupHours: 1.0,
        runHours: 3.5,
        startSlot: 18, // Tue 08:00
        endSlot: 26    // Tue 12:30 (9 slots = 4.5h)
      },
      {
        id: 'OP-107-2',
        jobId: 'JOB-107',
        seq: 20,
        name: 'Batch Drilling',
        machineId: 'M2',
        setupHours: 0.5,
        runHours: 2.0,
        startSlot: 46, // Wed 13:00
        endSlot: 50    // Wed 15:30 (5 slots = 2.5h)
      },

      // JOB-108: Precision Hydraulics (Normal)
      {
        id: 'OP-108-1',
        jobId: 'JOB-108',
        seq: 10,
        name: 'Fine Grinding & Turn',
        machineId: 'M3',
        setupHours: 1.0,
        runHours: 3.0,
        startSlot: 27, // Tue 12:30
        endSlot: 34    // Tue 16:30 (8 slots = 4.0h)
      },
      {
        id: 'OP-108-2',
        jobId: 'JOB-108',
        seq: 20,
        name: 'Spool Leak Testing QA',
        machineId: 'M5',
        setupHours: 0.5,
        runHours: 1.0,
        startSlot: 36, // Wed 08:00
        endSlot: 38    // Wed 09:30 (3 slots = 1.5h)
      }
    ];

    operations.forEach(op => {
      op.totalHours = (op.setupHours || 0) + (op.runHours || 0);
      op.totalSlots = Math.round(op.totalHours * SLOTS_PER_HOUR);
      op.setupSlots = Math.round((op.setupHours || 0) * SLOTS_PER_HOUR);
      op.runSlots = op.totalSlots - op.setupSlots;
      op.isUnscheduled = (op.startSlot === null || op.startSlot === undefined || op.startSlot < 0);
    });

    return {
      version: '1.0.0',
      timestamp: new Date().toISOString(),
      workshopName: 'AeroPrecision Machining Lab',
      machines,
      outages,
      jobs,
      operations
    };
  }

  function cloneState(state) {
    return JSON.parse(JSON.stringify(state));
  }

  // --- Schema Validation & Sanitization ---
  const ALLOWED_TOP_LEVEL_KEYS = new Set([
    'version', 'timestamp', 'workshopName', 'machines', 'outages', 'jobs', 'operations'
  ]);
  const ALLOWED_MACHINE_KEYS = new Set([
    'id', 'name', 'shortName', 'code', 'workCenter', 'hourlyRate', 'color', 'description'
  ]);
  const ALLOWED_OUTAGE_KEYS = new Set([
    'id', 'machineId', 'title', 'reason', 'dayIndex', 'startSlot', 'endSlot', 'type'
  ]);
  const ALLOWED_JOB_KEYS = new Set([
    'id', 'customer', 'partName', 'quantity', 'dueDateSlot', 'priority', 'color'
  ]);
  const ALLOWED_OP_KEYS = new Set([
    'id', 'jobId', 'seq', 'name', 'machineId', 'setupHours', 'runHours',
    'startSlot', 'endSlot', 'isUnscheduled', 'totalHours', 'totalSlots', 'setupSlots', 'runSlots'
  ]);

  const ID_REGEX = /^[A-Za-z0-9_-]{1,64}$/;
  const HEX_COLOR_REGEX = /^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/;

  function escapeHTML(str) {
    if (str === null || str === undefined) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function sanitizeColor(c) {
    if (typeof c === 'string' && HEX_COLOR_REGEX.test(c.trim())) {
      return c.trim().toLowerCase();
    }
    return '#2563eb';
  }

  function sanitizeString(str) {
    if (typeof str !== 'string') return '';
    return str.replace(/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/g, '').trim();
  }

  function validatePlanSchema(data) {
    const errors = [];
    if (!data || typeof data !== 'object' || Array.isArray(data)) {
      return { valid: false, errors: ['Plan data must be a valid JSON object.'] };
    }

    // 1. Top-Level Keys
    for (const key of Object.keys(data)) {
      if (!ALLOWED_TOP_LEVEL_KEYS.has(key)) {
        errors.push(`Unknown top-level field "${key}". Only standard ShiftBoard fields are permitted.`);
      }
    }

    // Version, timestamp, workshopName checks
    if (data.version !== undefined && (typeof data.version !== 'string' || data.version.length > 50)) {
      errors.push('Top-level "version" must be a string up to 50 characters.');
    }
    if (data.timestamp !== undefined && (typeof data.timestamp !== 'string' || data.timestamp.length > 50)) {
      errors.push('Top-level "timestamp" must be a string up to 50 characters.');
    }
    if (data.workshopName !== undefined && (typeof data.workshopName !== 'string' || data.workshopName.length > 200)) {
      errors.push('Top-level "workshopName" must be a string up to 200 characters.');
    }

    // 2. Machines Array
    if (!Array.isArray(data.machines)) {
      errors.push('Top-level "machines" must be an array.');
    } else if (data.machines.length === 0 || data.machines.length > 50) {
      errors.push('Top-level "machines" must contain between 1 and 50 machines.');
    }

    // 3. Jobs Array
    if (!Array.isArray(data.jobs)) {
      errors.push('Top-level "jobs" must be an array.');
    } else if (data.jobs.length === 0 || data.jobs.length > 500) {
      errors.push('Top-level "jobs" must contain between 1 and 500 jobs.');
    }

    // 4. Operations Array
    if (!Array.isArray(data.operations)) {
      errors.push('Top-level "operations" must be an array.');
    } else if (data.operations.length === 0 || data.operations.length > 2000) {
      errors.push('Top-level "operations" must contain between 1 and 2000 operations.');
    }

    // 5. Outages Array (optional, but if provided must be array)
    if (data.outages !== undefined && !Array.isArray(data.outages)) {
      errors.push('Top-level "outages" must be an array.');
    } else if (Array.isArray(data.outages) && data.outages.length > 200) {
      errors.push('Top-level "outages" must contain at most 200 outages.');
    }

    // If required collection arrays are missing, abort detailed inspection
    if (!Array.isArray(data.machines) || !Array.isArray(data.jobs) || !Array.isArray(data.operations)) {
      return { valid: false, errors };
    }

    // Validate Machines
    const machineIdSet = new Set();
    const cleanMachines = [];
    for (let i = 0; i < data.machines.length; i++) {
      const m = data.machines[i];
      const prefix = `Machine[${i}]`;
      if (!m || typeof m !== 'object' || Array.isArray(m)) {
        errors.push(`${prefix} must be an object.`);
        continue;
      }
      for (const k of Object.keys(m)) {
        if (!ALLOWED_MACHINE_KEYS.has(k)) {
          errors.push(`${prefix} contains unknown field "${k}".`);
        }
      }
      if (typeof m.id !== 'string' || !ID_REGEX.test(m.id)) {
        errors.push(`${prefix} "id" must be an alphanumeric identifier (1-64 chars, dash/underscore allowed).`);
      } else if (machineIdSet.has(m.id)) {
        errors.push(`${prefix} duplicate machine ID "${m.id}".`);
      } else {
        machineIdSet.add(m.id);
      }
      if (typeof m.name !== 'string' || m.name.trim().length === 0 || m.name.length > 100) {
        errors.push(`${prefix} "name" must be a non-empty string up to 100 characters.`);
      }
      if (typeof m.shortName !== 'string' || m.shortName.trim().length === 0 || m.shortName.length > 100) {
        errors.push(`${prefix} "shortName" must be a non-empty string up to 100 characters.`);
      }
      if (typeof m.code !== 'string' || m.code.trim().length === 0 || m.code.length > 64) {
        errors.push(`${prefix} "code" must be a non-empty string up to 64 characters.`);
      }
      if (typeof m.workCenter !== 'string' || m.workCenter.trim().length === 0 || m.workCenter.length > 100) {
        errors.push(`${prefix} "workCenter" must be a non-empty string up to 100 characters.`);
      }
      if (typeof m.hourlyRate !== 'number' || !Number.isFinite(m.hourlyRate) || m.hourlyRate <= 0 || m.hourlyRate > 100000) {
        errors.push(`${prefix} "hourlyRate" must be a positive number between 0 and 100,000.`);
      }
      if (typeof m.color !== 'string' || !HEX_COLOR_REGEX.test(m.color.trim())) {
        errors.push(`${prefix} "color" must be a valid hex color code (e.g. #2563eb).`);
      }
      if (m.description !== undefined && (typeof m.description !== 'string' || m.description.length > 500)) {
        errors.push(`${prefix} "description" must be a string up to 500 characters.`);
      }

      cleanMachines.push({
        id: sanitizeString(m.id),
        name: sanitizeString(m.name),
        shortName: sanitizeString(m.shortName),
        code: sanitizeString(m.code),
        workCenter: sanitizeString(m.workCenter),
        hourlyRate: Number(m.hourlyRate),
        color: sanitizeColor(m.color),
        description: sanitizeString(m.description || '')
      });
    }

    // Validate Jobs
    const jobIdSet = new Set();
    const cleanJobs = [];
    for (let i = 0; i < data.jobs.length; i++) {
      const j = data.jobs[i];
      const prefix = `Job[${i}]`;
      if (!j || typeof j !== 'object' || Array.isArray(j)) {
        errors.push(`${prefix} must be an object.`);
        continue;
      }
      for (const k of Object.keys(j)) {
        if (!ALLOWED_JOB_KEYS.has(k)) {
          errors.push(`${prefix} contains unknown field "${k}".`);
        }
      }
      if (typeof j.id !== 'string' || !ID_REGEX.test(j.id)) {
        errors.push(`${prefix} "id" must be an alphanumeric identifier (1-64 chars, dash/underscore allowed).`);
      } else if (jobIdSet.has(j.id)) {
        errors.push(`${prefix} duplicate job ID "${j.id}".`);
      } else {
        jobIdSet.add(j.id);
      }
      if (typeof j.customer !== 'string' || j.customer.trim().length === 0 || j.customer.length > 100) {
        errors.push(`${prefix} "customer" must be a non-empty string up to 100 characters.`);
      }
      if (typeof j.partName !== 'string' || j.partName.trim().length === 0 || j.partName.length > 100) {
        errors.push(`${prefix} "partName" must be a non-empty string up to 100 characters.`);
      }
      if (!Number.isInteger(j.quantity) || j.quantity < 1 || j.quantity > 1000000) {
        errors.push(`${prefix} "quantity" must be an integer between 1 and 1,000,000.`);
      }
      if (!Number.isInteger(j.dueDateSlot) || j.dueDateSlot < 0 || j.dueDateSlot >= TOTAL_SLOTS) {
        errors.push(`${prefix} "dueDateSlot" must be an integer slot between 0 and ${TOTAL_SLOTS - 1}.`);
      }
      if (typeof j.priority !== 'string' || !PRIORITIES[j.priority]) {
        errors.push(`${prefix} "priority" must be one of: ${Object.keys(PRIORITIES).join(', ')}.`);
      }
      if (typeof j.color !== 'string' || !HEX_COLOR_REGEX.test(j.color.trim())) {
        errors.push(`${prefix} "color" must be a valid hex color code (e.g. #2563eb).`);
      }

      cleanJobs.push({
        id: sanitizeString(j.id),
        customer: sanitizeString(j.customer),
        partName: sanitizeString(j.partName),
        quantity: j.quantity,
        dueDateSlot: j.dueDateSlot,
        priority: j.priority,
        color: sanitizeColor(j.color)
      });
    }

    // Validate Outages
    const outageIdSet = new Set();
    const cleanOutages = [];
    const outages = data.outages || [];
    for (let i = 0; i < outages.length; i++) {
      const o = outages[i];
      const prefix = `Outage[${i}]`;
      if (!o || typeof o !== 'object' || Array.isArray(o)) {
        errors.push(`${prefix} must be an object.`);
        continue;
      }
      for (const k of Object.keys(o)) {
        if (!ALLOWED_OUTAGE_KEYS.has(k)) {
          errors.push(`${prefix} contains unknown field "${k}".`);
        }
      }
      if (typeof o.id !== 'string' || !ID_REGEX.test(o.id)) {
        errors.push(`${prefix} "id" must be an alphanumeric identifier (1-64 chars, dash/underscore allowed).`);
      } else if (outageIdSet.has(o.id)) {
        errors.push(`${prefix} duplicate outage ID "${o.id}".`);
      } else {
        outageIdSet.add(o.id);
      }
      if (typeof o.machineId !== 'string' || !machineIdSet.has(o.machineId)) {
        errors.push(`${prefix} references non-existent machine "${o.machineId}".`);
      }
      if (typeof o.title !== 'string' || o.title.trim().length === 0 || o.title.length > 150) {
        errors.push(`${prefix} "title" must be a non-empty string up to 150 characters.`);
      }
      if (typeof o.reason !== 'string' || o.reason.length > 500) {
        errors.push(`${prefix} "reason" must be a string up to 500 characters.`);
      }
      if (!Number.isInteger(o.dayIndex) || o.dayIndex < 0 || o.dayIndex >= TOTAL_DAYS) {
        errors.push(`${prefix} "dayIndex" must be an integer between 0 and ${TOTAL_DAYS - 1}.`);
      }
      if (!Number.isInteger(o.startSlot) || o.startSlot < 0 || o.startSlot >= TOTAL_SLOTS) {
        errors.push(`${prefix} "startSlot" must be an integer between 0 and ${TOTAL_SLOTS - 1}.`);
      }
      if (!Number.isInteger(o.endSlot) || o.endSlot < 0 || o.endSlot >= TOTAL_SLOTS) {
        errors.push(`${prefix} "endSlot" must be an integer between 0 and ${TOTAL_SLOTS - 1}.`);
      } else if (Number.isInteger(o.startSlot) && o.endSlot < o.startSlot) {
        errors.push(`${prefix} "endSlot" (${o.endSlot}) must be >= "startSlot" (${o.startSlot}).`);
      }
      if (o.type !== undefined && (typeof o.type !== 'string' || o.type.length > 50)) {
        errors.push(`${prefix} "type" must be a string up to 50 characters.`);
      }

      cleanOutages.push({
        id: sanitizeString(o.id),
        machineId: sanitizeString(o.machineId),
        title: sanitizeString(o.title),
        reason: sanitizeString(o.reason || ''),
        dayIndex: o.dayIndex,
        startSlot: o.startSlot,
        endSlot: o.endSlot,
        type: sanitizeString(o.type || 'maintenance')
      });
    }

    // Validate Operations
    const opIdSet = new Set();
    const cleanOperations = [];
    for (let i = 0; i < data.operations.length; i++) {
      const op = data.operations[i];
      const prefix = `Operation[${i}]`;
      if (!op || typeof op !== 'object' || Array.isArray(op)) {
        errors.push(`${prefix} must be an object.`);
        continue;
      }
      for (const k of Object.keys(op)) {
        if (!ALLOWED_OP_KEYS.has(k)) {
          errors.push(`${prefix} contains unknown field "${k}".`);
        }
      }
      if (typeof op.id !== 'string' || !ID_REGEX.test(op.id)) {
        errors.push(`${prefix} "id" must be an alphanumeric identifier (1-64 chars, dash/underscore allowed).`);
      } else if (opIdSet.has(op.id)) {
        errors.push(`${prefix} duplicate operation ID "${op.id}".`);
      } else {
        opIdSet.add(op.id);
      }
      if (typeof op.jobId !== 'string' || !jobIdSet.has(op.jobId)) {
        errors.push(`${prefix} references non-existent job "${op.jobId}".`);
      }
      if (!Number.isInteger(op.seq) || op.seq < 1) {
        errors.push(`${prefix} "seq" must be a positive integer >= 1.`);
      }
      if (typeof op.name !== 'string' || op.name.trim().length === 0 || op.name.length > 150) {
        errors.push(`${prefix} "name" must be a non-empty string up to 150 characters.`);
      }
      if (typeof op.machineId !== 'string' || !machineIdSet.has(op.machineId)) {
        errors.push(`${prefix} references non-existent machine "${op.machineId}".`);
      }
      if (typeof op.setupHours !== 'number' || !Number.isFinite(op.setupHours) || op.setupHours < 0 || op.setupHours > 45) {
        errors.push(`${prefix} "setupHours" must be a finite number between 0 and 45.`);
      }
      if (typeof op.runHours !== 'number' || !Number.isFinite(op.runHours) || op.runHours < 0 || op.runHours > 45) {
        errors.push(`${prefix} "runHours" must be a finite number between 0 and 45.`);
      } else if ((op.setupHours || 0) + (op.runHours || 0) <= 0) {
        errors.push(`${prefix} total duration (setupHours + runHours) must be > 0.`);
      }

      const isUnscheduled = op.isUnscheduled === true ||
        op.startSlot === null || op.startSlot === undefined || op.startSlot < 0;

      let startSlot = null;
      let endSlot = null;
      if (!isUnscheduled) {
        if (!Number.isInteger(op.startSlot) || op.startSlot < 0 || op.startSlot >= TOTAL_SLOTS) {
          errors.push(`${prefix} scheduled "startSlot" must be an integer between 0 and ${TOTAL_SLOTS - 1}.`);
        } else {
          startSlot = op.startSlot;
        }
        if (!Number.isInteger(op.endSlot) || op.endSlot < 0) {
          errors.push(`${prefix} scheduled "endSlot" must be an integer.`);
        } else if (Number.isInteger(op.startSlot) && op.endSlot < op.startSlot) {
          errors.push(`${prefix} scheduled "endSlot" (${op.endSlot}) must be >= "startSlot" (${op.startSlot}).`);
        } else {
          endSlot = op.endSlot;
        }
      }

      const setupH = Number(op.setupHours || 0);
      const runH = Number(op.runHours || 0);
      const totalH = setupH + runH;
      const setupSlots = Math.round(setupH * SLOTS_PER_HOUR);
      const totalSlots = Math.round(totalH * SLOTS_PER_HOUR);
      const runSlots = totalSlots - setupSlots;

      cleanOperations.push({
        id: sanitizeString(op.id),
        jobId: sanitizeString(op.jobId),
        seq: op.seq,
        name: sanitizeString(op.name),
        machineId: sanitizeString(op.machineId),
        setupHours: setupH,
        runHours: runH,
        startSlot,
        endSlot,
        isUnscheduled,
        totalHours: totalH,
        totalSlots,
        setupSlots,
        runSlots
      });
    }

    if (errors.length > 0) {
      return { valid: false, errors };
    }

    const cleanState = {
      version: sanitizeString(data.version || '1.0.0'),
      timestamp: sanitizeString(data.timestamp || new Date().toISOString()),
      workshopName: sanitizeString(data.workshopName || 'AeroPrecision Machining Lab'),
      machines: cleanMachines,
      outages: cleanOutages,
      jobs: cleanJobs,
      operations: cleanOperations
    };

    return { valid: true, errors: [], sanitizedState: cleanState };
  }

  function validateSchedule(state) {
    const conflicts = [];
    const { machines, outages, jobs, operations } = state;
    const machineMap = new Map(machines.map(m => [m.id, m]));

    const opsByMachine = new Map();
    const opsByJob = new Map();

    machines.forEach(m => opsByMachine.set(m.id, []));
    jobs.forEach(j => opsByJob.set(j.id, []));

    operations.forEach(op => {
      if (op.isUnscheduled || op.startSlot === null || op.startSlot === undefined || op.startSlot < 0) {
        return;
      }
      if (opsByMachine.has(op.machineId)) {
        opsByMachine.get(op.machineId).push(op);
      }
      if (opsByJob.has(op.jobId)) {
        opsByJob.get(op.jobId).push(op);
      }

      if (op.startSlot < 0 || op.endSlot >= TOTAL_SLOTS) {
        conflicts.push({
          id: 'bound-' + op.id,
          type: 'out_of_bounds',
          severity: 'critical',
          operationId: op.id,
          jobId: op.jobId,
          machineId: op.machineId,
          title: 'Schedule Horizon Violation',
          description: 'Operation ' + op.name + ' (' + op.id + ') exceeds the 5-day working horizon (finishes at ' + slotToTimeEnd(op.endSlot).displayStr + ', past Friday 17:00). Move earlier or unschedule.',
          suggestedAction: 'Move ' + op.id + ' earlier or unschedule to prevent week overflow.',
          affectedSlots: [op.startSlot, Math.min(TOTAL_SLOTS - 1, op.endSlot)]
        });
      }
    });

    // 1. Machine Overlaps
    opsByMachine.forEach((ops, machineId) => {
      const machine = machineMap.get(machineId);
      for (let i = 0; i < ops.length; i++) {
        for (let j = i + 1; j < ops.length; j++) {
          const opA = ops[i];
          const opB = ops[j];
          const overlapStart = Math.max(opA.startSlot, opB.startSlot);
          const overlapEnd = Math.min(opA.endSlot, opB.endSlot);

          if (overlapStart <= overlapEnd) {
            conflicts.push({
              id: 'overlap-' + opA.id + '-' + opB.id,
              type: 'machine_overlap',
              severity: 'critical',
              operationId: opB.id,
              conflictingOperationId: opA.id,
              jobId: opB.jobId,
              machineId,
              machineName: machine ? machine.name : machineId,
              title: 'Machine Capacity Overlap on ' + (machine ? machine.shortName : machineId),
              description: 'Operations ' + opA.id + ' (' + opA.name + ') and ' + opB.id + ' (' + opB.name + ') collide on ' + (machine ? machine.shortName : machineId) + ' between ' + slotToTime(overlapStart).displayStr + ' and ' + slotToTimeEnd(overlapEnd).displayStr + '.',
              affectedSlots: [overlapStart, overlapEnd],
              suggestedAction: 'Reschedule ' + opB.id + ' to start at or after ' + slotToTimeEnd(opA.endSlot).displayStr + '.'
            });
          }
        }
      }
    });

    // 2. Machine Outages
    outages.forEach(outage => {
      const machineOps = opsByMachine.get(outage.machineId) || [];
      const machine = machineMap.get(outage.machineId);

      machineOps.forEach(op => {
        const overlapStart = Math.max(op.startSlot, outage.startSlot);
        const overlapEnd = Math.min(op.endSlot, outage.endSlot);

        if (overlapStart <= overlapEnd) {
          conflicts.push({
            id: 'outage-' + outage.id + '-' + op.id,
            type: 'outage_overlap',
            severity: 'critical',
            operationId: op.id,
            outageId: outage.id,
            jobId: op.jobId,
            machineId: outage.machineId,
            machineName: machine ? machine.name : outage.machineId,
            title: 'Machine Outage Conflict on ' + (machine ? machine.shortName : outage.machineId),
            description: 'Operation ' + op.id + ' (' + op.name + ') overlaps with planned outage "' + outage.title + '" on ' + (machine ? machine.shortName : outage.machineId) + ' between ' + slotToTime(overlapStart).displayStr + ' and ' + slotToTimeEnd(overlapEnd).displayStr + '.',
            affectedSlots: [overlapStart, overlapEnd],
            suggestedAction: 'Move ' + op.id + ' after outage ends at ' + slotToTimeEnd(outage.endSlot).displayStr + '.'
          });
        }
      });
    });

    // 3. Precedence
    jobs.forEach(job => {
      const jobOps = operations.filter(o => o.jobId === job.id);
      jobOps.sort((a, b) => a.seq - b.seq);

      for (let k = 1; k < jobOps.length; k++) {
        const prevOp = jobOps[k - 1];
        const currOp = jobOps[k];

        if (prevOp.isUnscheduled || currOp.isUnscheduled) {
          continue;
        }

        if (currOp.startSlot <= prevOp.endSlot) {
          conflicts.push({
            id: 'precedence-' + job.id + '-' + prevOp.id + '-' + currOp.id,
            type: 'precedence_violation',
            severity: 'critical',
            operationId: currOp.id,
            predecessorId: prevOp.id,
            jobId: job.id,
            jobName: job.partName,
            title: 'Precedence Violation in Job ' + job.id,
            description: 'Operation Seq ' + currOp.seq + ' (' + currOp.name + ') starts at ' + slotToTime(currOp.startSlot).displayStr + ', but predecessor Seq ' + prevOp.seq + ' (' + prevOp.name + ') is not finished until ' + slotToTimeEnd(prevOp.endSlot).displayStr + '.',
            affectedSlots: [currOp.startSlot, prevOp.endSlot],
            suggestedAction: 'Delay ' + currOp.id + ' to start at or after ' + slotToTimeEnd(prevOp.endSlot).displayStr + '.'
          });
        }
      }
    });

    // 4. Lateness
    jobs.forEach(job => {
      const jobOps = operations.filter(o => o.jobId === job.id);
      jobOps.sort((a, b) => a.seq - b.seq);

      const hasUnscheduled = jobOps.some(o => o.isUnscheduled);
      const lastOp = jobOps[jobOps.length - 1];

      if (!hasUnscheduled && lastOp && lastOp.endSlot > job.dueDateSlot) {
        const tardinessSlots = lastOp.endSlot - job.dueDateSlot;
        const tardinessHours = tardinessSlots * SLOT_DURATION_HOURS;
        conflicts.push({
          id: 'late-' + job.id,
          type: 'tardiness',
          severity: 'warning',
          jobId: job.id,
          operationId: lastOp.id,
          title: 'Late Order Delivery: ' + job.id + ' (' + job.customer + ')',
          description: 'Job ' + job.id + ' finishes at ' + slotToTimeEnd(lastOp.endSlot).displayStr + ', which is ' + tardinessHours.toFixed(1) + 'h past customer due date (' + slotToTimeEnd(job.dueDateSlot).displayStr + ').',
          affectedSlots: [job.dueDateSlot, lastOp.endSlot],
          suggestedAction: 'Expedite operations or reassign to faster / earlier machine slots.'
        });
      }
    });

    return conflicts;
  }

  function calculateKPIs(state) {
    const { machines, outages, jobs, operations } = state;
    const conflicts = validateSchedule(state);

    const machineKPIs = {};
    let totalWorkshopAvailableHours = 0;
    let totalWorkshopScheduledHours = 0;

    machines.forEach(machine => {
      const machineOutageSlots = outages
        .filter(o => o.machineId === machine.id)
        .reduce((acc, o) => acc + (o.endSlot - o.startSlot + 1), 0);

      const availableSlots = Math.max(0, TOTAL_SLOTS - machineOutageSlots);
      const availableHours = availableSlots * SLOT_DURATION_HOURS;

      const scheduledOps = operations.filter(
        o => o.machineId === machine.id && !o.isUnscheduled && o.startSlot !== null && o.startSlot >= 0
      );

      const setupHours = scheduledOps.reduce((acc, o) => acc + (o.setupHours || 0), 0);
      const runHours = scheduledOps.reduce((acc, o) => acc + (o.runHours || 0), 0);
      const totalScheduledHours = setupHours + runHours;

      const utilizationPercent = availableHours > 0 ? (totalScheduledHours / availableHours) * 100 : 0;

      machineKPIs[machine.id] = {
        machineId: machine.id,
        name: machine.name,
        shortName: machine.shortName,
        availableSlots,
        availableHours,
        outageHours: machineOutageSlots * SLOT_DURATION_HOURS,
        setupHours,
        runHours,
        totalScheduledHours,
        utilizationPercent: Math.min(100, parseFloat(utilizationPercent.toFixed(1)))
      };

      totalWorkshopAvailableHours += availableHours;
      totalWorkshopScheduledHours += totalScheduledHours;
    });

    const overallUtilizationPercent = totalWorkshopAvailableHours > 0
      ? (totalWorkshopScheduledHours / totalWorkshopAvailableHours) * 100
      : 0;

    let onTimeJobsCount = 0;
    let lateJobsCount = 0;
    let unscheduledJobsCount = 0;
    let totalTardinessHours = 0;
    const jobStatuses = [];

    jobs.forEach(job => {
      const jobOps = operations.filter(o => o.jobId === job.id);
      jobOps.sort((a, b) => a.seq - b.seq);

      const anyUnscheduled = jobOps.some(o => o.isUnscheduled || o.startSlot === null || o.startSlot < 0);
      const lastOp = jobOps[jobOps.length - 1];

      let finishSlot = null;
      let status = 'UNSCHEDULED';
      let tardinessHours = 0;

      if (!anyUnscheduled && lastOp) {
        finishSlot = lastOp.endSlot;
        if (finishSlot <= job.dueDateSlot) {
          status = 'ON_TIME';
          onTimeJobsCount++;
        } else {
          status = 'LATE';
          lateJobsCount++;
          tardinessHours = (finishSlot - job.dueDateSlot) * SLOT_DURATION_HOURS;
          totalTardinessHours += tardinessHours;
        }
      } else {
        unscheduledJobsCount++;
      }

      jobStatuses.push({
        jobId: job.id,
        customer: job.customer,
        partName: job.partName,
        priority: job.priority,
        dueDateSlot: job.dueDateSlot,
        dueDateStr: slotToTimeEnd(job.dueDateSlot).displayStr,
        finishSlot,
        finishDateStr: finishSlot !== null ? slotToTimeEnd(finishSlot).displayStr : 'N/A',
        status,
        tardinessHours: parseFloat(tardinessHours.toFixed(1))
      });
    });

    const unscheduledOperationsCount = operations.filter(o => o.isUnscheduled).length;

    const scheduledEndSlots = operations
      .filter(o => !o.isUnscheduled && o.endSlot !== null)
      .map(o => o.endSlot);
    const maxEndSlot = scheduledEndSlots.length > 0 ? Math.max(...scheduledEndSlots) : 0;
    const makespanHours = (maxEndSlot + 1) * SLOT_DURATION_HOURS;

    return {
      overallUtilizationPercent: parseFloat(overallUtilizationPercent.toFixed(1)),
      totalWorkshopAvailableHours: parseFloat(totalWorkshopAvailableHours.toFixed(1)),
      totalWorkshopScheduledHours: parseFloat(totalWorkshopScheduledHours.toFixed(1)),
      machineKPIs,
      totalJobs: jobs.length,
      onTimeJobsCount,
      lateJobsCount,
      unscheduledJobsCount,
      onTimeRatePercent: jobs.length > 0 ? parseFloat(((onTimeJobsCount / jobs.length) * 100).toFixed(1)) : 0,
      totalTardinessHours: parseFloat(totalTardinessHours.toFixed(1)),
      unscheduledOperationsCount,
      makespanHours: parseFloat(makespanHours.toFixed(1)),
      makespanEndStr: slotToTimeEnd(maxEndSlot).displayStr,
      criticalConflictsCount: conflicts.filter(c => c.severity === 'critical').length,
      warningConflictsCount: conflicts.filter(c => c.severity === 'warning').length,
      totalConflictsCount: conflicts.length,
      jobStatuses
    };
  }

  function proposeSchedule(state, options) {
    options = options || {};
    const newState = cloneState(state);
    const { machines, outages, jobs, operations } = newState;

    const machineGrid = {};
    machines.forEach(m => {
      machineGrid[m.id] = new Array(TOTAL_SLOTS).fill(null);
    });

    outages.forEach(outage => {
      const grid = machineGrid[outage.machineId];
      if (grid) {
        for (let s = outage.startSlot; s <= outage.endSlot && s < TOTAL_SLOTS; s++) {
          grid[s] = { type: 'outage', id: outage.id, title: outage.title };
        }
      }
    });

    const changes = [];
    const prevOpMap = new Map(state.operations.map(o => [o.id, { startSlot: o.startSlot, endSlot: o.endSlot, isUnscheduled: o.isUnscheduled }]));

    const sortedJobs = [...jobs].sort((a, b) => {
      const rankA = PRIORITIES[a.priority] ? PRIORITIES[a.priority].rank : 0;
      const rankB = PRIORITIES[b.priority] ? PRIORITIES[b.priority].rank : 0;
      if (rankB !== rankA) return rankB - rankA;
      if (a.dueDateSlot !== b.dueDateSlot) return a.dueDateSlot - b.dueDateSlot;
      return a.id.localeCompare(b.id);
    });

    sortedJobs.forEach(job => {
      const jobOps = operations.filter(o => o.jobId === job.id);
      jobOps.sort((a, b) => a.seq - b.seq);

      let earliestAllowedStart = 0;

      jobOps.forEach(op => {
        const requiredSlots = op.totalSlots || Math.round(((op.setupHours || 0) + (op.runHours || 0)) * SLOTS_PER_HOUR);
        const grid = machineGrid[op.machineId];

        if (!grid) {
          op.isUnscheduled = true;
          op.startSlot = null;
          op.endSlot = null;
          return;
        }

        let foundStart = -1;
        for (let s = earliestAllowedStart; s <= TOTAL_SLOTS - requiredSlots; s++) {
          let feasible = true;
          for (let k = 0; k < requiredSlots; k++) {
            if (grid[s + k] !== null) {
              feasible = false;
              break;
            }
          }
          if (feasible) {
            foundStart = s;
            break;
          }
        }

        if (foundStart !== -1) {
          const foundEnd = foundStart + requiredSlots - 1;
          op.startSlot = foundStart;
          op.endSlot = foundEnd;
          op.isUnscheduled = false;

          for (let s = foundStart; s <= foundEnd; s++) {
            grid[s] = { type: 'operation', id: op.id, jobId: op.jobId };
          }

          earliestAllowedStart = foundEnd + 1;
        } else {
          op.startSlot = null;
          op.endSlot = null;
          op.isUnscheduled = true;
          earliestAllowedStart = TOTAL_SLOTS;
        }

        const prev = prevOpMap.get(op.id);
        if (prev) {
          if (prev.isUnscheduled !== op.isUnscheduled || prev.startSlot !== op.startSlot) {
            changes.push({
              operationId: op.id,
              jobId: op.jobId,
              name: op.name,
              from: prev.isUnscheduled ? 'Unscheduled' : formatSlotRange(prev.startSlot, prev.endSlot),
              to: op.isUnscheduled ? 'Unscheduled (Capacity Exceeded)' : formatSlotRange(op.startSlot, op.endSlot)
            });
          }
        }
      });
    });

    return {
      newState,
      changes,
      summaryText: changes.length === 0
        ? 'Schedule is already optimal. No timing changes made.'
        : 'Automatically rescheduled ' + changes.length + ' operation(s) according to priority and due dates.'
    };
  }

  function moveOperation(state, operationId, targetMachineId, targetStartSlot) {
    const newState = cloneState(state);
    const op = newState.operations.find(o => o.id === operationId);
    if (!op) throw new Error('Operation ' + operationId + ' not found.');

    const slots = op.totalSlots || Math.round(((op.setupHours || 0) + (op.runHours || 0)) * SLOTS_PER_HOUR);

    if (targetStartSlot === null || targetStartSlot === undefined || targetStartSlot < 0) {
      op.isUnscheduled = true;
      op.startSlot = null;
      op.endSlot = null;
    } else {
      op.isUnscheduled = false;
      op.machineId = targetMachineId || op.machineId;
      op.startSlot = targetStartSlot;
      op.endSlot = targetStartSlot + slots - 1;
    }

    return newState;
  }

  function autoResolveConflicts(state) {
    return proposeSchedule(state);
  }

  function applyUrgentOrderScenario(state) {
    const newState = cloneState(state);

    if (newState.jobs.some(j => j.id === 'JOB-901')) {
      return { newState, applied: false, message: 'Urgent Order JOB-901 is already present on the board.' };
    }

    const urgentJob = {
      id: 'JOB-901',
      customer: 'AeroTech Defense Emergency',
      partName: 'A320 Flap Actuator Mount (AOG)',
      quantity: 4,
      dueDateSlot: 44, // Wed 12:00
      priority: 'URGENT',
      color: '#ef4444'
    };

    const urgentOps = [
      {
        id: 'OP-901-1',
        jobId: 'JOB-901',
        seq: 10,
        name: 'Urgent Turning Hub',
        machineId: 'M3',
        setupHours: 1.0,
        runHours: 2.0,
        totalHours: 3.0,
        totalSlots: 6,
        setupSlots: 2,
        runSlots: 4,
        startSlot: 18, // Tue 08:00
        endSlot: 23,
        isUnscheduled: false
      },
      {
        id: 'OP-901-2',
        jobId: 'JOB-901',
        seq: 20,
        name: 'Emergency 5-Axis Mill',
        machineId: 'M1',
        setupHours: 1.0,
        runHours: 3.0,
        totalHours: 4.0,
        totalSlots: 8,
        setupSlots: 2,
        runSlots: 6,
        startSlot: 24, // Tue 11:00
        endSlot: 31,
        isUnscheduled: false
      },
      {
        id: 'OP-901-3',
        jobId: 'JOB-901',
        seq: 30,
        name: 'Flight-Cert CMM QA',
        machineId: 'M5',
        setupHours: 0.5,
        runHours: 1.0,
        totalHours: 1.5,
        totalSlots: 3,
        setupSlots: 1,
        runSlots: 2,
        startSlot: 36, // Wed 08:00
        endSlot: 38,
        isUnscheduled: false
      }
    ];

    newState.jobs.push(urgentJob);
    newState.operations.push(...urgentOps);

    return {
      newState,
      applied: true,
      message: 'Urgent Order JOB-901 (AeroTech Emergency) introduced! It creates immediate capacity competition on Lathe M3, Mill M1, and CMM M5.'
    };
  }

  function applyExtendedOutageScenario(state) {
    const newState = cloneState(state);

    const m1Outage = newState.outages.find(o => o.id === 'OUT-01');
    if (m1Outage) {
      m1Outage.endSlot = 52; // Extend through Wednesday 16:30 (collides with OP-106-2)
      m1Outage.title = 'Extended Outage: Spindle Drive Failure';
      m1Outage.reason = 'Emergency bearing seizure requiring replacement parts and recalibration';
    } else {
      newState.outages.push({
        id: 'OUT-02',
        machineId: 'M1',
        title: 'Emergency Breakdown: Spindle Drive Failure',
        reason: 'Unplanned motor seizure requiring recalibration',
        dayIndex: 2,
        startSlot: 36,
        endSlot: 52,
        type: 'breakdown'
      });
    }

    return {
      newState,
      applied: true,
      message: 'Extended Outage applied to Haas UMC-750 (M1) through Wednesday afternoon. Operations scheduled in this window now show critical conflicts!'
    };
  }

  function generateMarkdownExport(state) {
    const kpis = calculateKPIs(state);
    const conflicts = validateSchedule(state);
    const { machines, jobs, operations, outages } = state;
    const jobMap = new Map(jobs.map(j => [j.id, j]));

    let md = '# ShiftBoard Production Schedule & Dispatch Report\n';
    md += '*Generated: ' + new Date().toLocaleString() + '*\n\n';

    md += '## 1. Executive Summary & KPIs\n\n';
    md += '| Metric | Value | Reference Target |\n';
    md += '| :--- | :--- | :--- |\n';
    md += '| **Overall Workshop Utilization** | **' + kpis.overallUtilizationPercent + '%** | Target: > 75% |\n';
    md += '| **Total Available Operating Hours** | ' + kpis.totalWorkshopAvailableHours + 'h | 5 machines × 45h minus outages |\n';
    md += '| **Total Scheduled Work Hours** | ' + kpis.totalWorkshopScheduledHours + 'h | Setup + Run hours |\n';
    md += '| **On-Time Delivery Rate** | **' + kpis.onTimeRatePercent + '%** (' + kpis.onTimeJobsCount + '/' + kpis.totalJobs + ' jobs) | Target: 100% |\n';
    md += '| **Late Orders** | ' + kpis.lateJobsCount + ' (' + kpis.totalTardinessHours + 'h total tardiness) | Target: 0 |\n';
    md += '| **Unscheduled Operations** | ' + kpis.unscheduledOperationsCount + ' | Target: 0 |\n';
    md += '| **Schedule Makespan** | ' + kpis.makespanHours + 'h (Finishes ' + kpis.makespanEndStr + ') | Within 45h week |\n';
    md += '| **Active Conflicts** | ' + kpis.totalConflictsCount + ' (' + kpis.criticalConflictsCount + ' critical) | Target: 0 |\n\n';

    md += '## 2. Machine Utilization Breakdown\n\n';
    md += '| Machine | Work Center | Available (h) | Outage (h) | Setup (h) | Run (h) | Utilization |\n';
    md += '| :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n';
    machines.forEach(m => {
      const k = kpis.machineKPIs[m.id];
      md += '| **' + m.shortName + '** | ' + m.workCenter + ' | ' + k.availableHours + 'h | ' + k.outageHours + 'h | ' + k.setupHours + 'h | ' + k.runHours + 'h | **' + k.utilizationPercent + '%** |\n';
    });
    md += '\n';

    md += '## 3. Customer Job Status & Planned Completion\n\n';
    md += '| Job ID | Customer | Part Name | Priority | Due Date | Planned Finish | Status |\n';
    md += '| :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n';
    kpis.jobStatuses.forEach(js => {
      const p = PRIORITIES[js.priority] || { icon: '', label: js.priority };
      const statusLabel = js.status === 'ON_TIME' ? '✅ ON TIME' : (js.status === 'LATE' ? ('⚠️ LATE (+' + js.tardinessHours + 'h)') : '❌ UNSCHEDULED');
      md += '| **' + js.jobId + '** | ' + js.customer + ' | ' + js.partName + ' | ' + p.icon + ' ' + p.label + ' | ' + js.dueDateStr + ' | ' + js.finishDateStr + ' | ' + statusLabel + ' |\n';
    });
    md += '\n';

    md += '## 4. Detailed Machine Dispatch Timetable\n\n';
    machines.forEach(m => {
      md += '### ' + m.name + ' (' + m.workCenter + ')\n\n';
      const machineOps = operations.filter(o => o.machineId === m.id && !o.isUnscheduled);
      machineOps.sort((a, b) => a.startSlot - b.startSlot);

      const machineOutages = outages.filter(o => o.machineId === m.id);

      if (machineOps.length === 0 && machineOutages.length === 0) {
        md += '*No operations or outages scheduled on this machine.*\n\n';
        return;
      }

      md += '| Time Window | Item / Operation | Job | Customer | Setup / Run | Status |\n';
      md += '| :--- | :--- | :--- | :--- | :--- | :--- |\n';

      const timelineItems = [
        ...machineOutages.map(o => ({ type: 'outage', startSlot: o.startSlot, endSlot: o.endSlot, data: o })),
        ...machineOps.map(o => ({ type: 'op', startSlot: o.startSlot, endSlot: o.endSlot, data: o }))
      ].sort((a, b) => a.startSlot - b.startSlot);

      timelineItems.forEach(item => {
        const timeRange = formatSlotRange(item.startSlot, item.endSlot);
        if (item.type === 'outage') {
          md += '| ' + timeRange + ' | 🔧 **PLANNED OUTAGE: ' + item.data.title + '** | - | *Maintenance* | ' + ((item.endSlot - item.startSlot + 1) * 0.5) + 'h | [BLOCKED] |\n';
        } else {
          const op = item.data;
          const job = jobMap.get(op.jobId);
          const cust = job ? job.customer : '-';
          md += '| ' + timeRange + ' | **' + op.id + ': ' + op.name + '** | ' + op.jobId + ' | ' + cust + ' | Setup: ' + op.setupHours + 'h, Run: ' + op.runHours + 'h | [SCHEDULED] |\n';
        }
      });
      md += '\n';
    });

    if (conflicts.length > 0) {
      md += '## 5. Active Conflicts & Constraint Warnings\n\n';
      conflicts.forEach((c, idx) => {
        md += (idx + 1) + '. **[' + c.severity.toUpperCase() + '] ' + c.title + '**\n';
        md += '   - Description: ' + c.description + '\n';
        if (c.suggestedAction) md += '   - Suggested Action: ' + c.suggestedAction + '\n';
        md += '\n';
      });
    }

    return md;
  }

  function generateCSVExport(state) {
    const { jobs, operations, machines } = state;
    const jobMap = new Map(jobs.map(j => [j.id, j]));
    const machineMap = new Map(machines.map(m => [m.id, m]));

    const headers = [
      'Job ID',
      'Customer',
      'Part Name',
      'Priority',
      'Job Due Date',
      'Operation ID',
      'Sequence',
      'Operation Name',
      'Machine ID',
      'Machine Name',
      'Setup Hours',
      'Run Hours',
      'Total Hours',
      'Start Time',
      'End Time',
      'Is Unscheduled'
    ];

    const rows = operations.map(op => {
      const job = jobMap.get(op.jobId) || {};
      const machine = machineMap.get(op.machineId) || {};
      const startStr = op.isUnscheduled ? 'N/A' : slotToTime(op.startSlot).displayStr;
      const endStr = op.isUnscheduled ? 'N/A' : slotToTimeEnd(op.endSlot).displayStr;
      const dueStr = job.dueDateSlot !== undefined ? slotToTimeEnd(job.dueDateSlot).displayStr : 'N/A';

      return [
        '"' + op.jobId + '"',
        '"' + (job.customer || '') + '"',
        '"' + (job.partName || '') + '"',
        '"' + (job.priority || '') + '"',
        '"' + dueStr + '"',
        '"' + op.id + '"',
        op.seq,
        '"' + op.name + '"',
        '"' + op.machineId + '"',
        '"' + (machine.shortName || op.machineId) + '"',
        op.setupHours || 0,
        op.runHours || 0,
        op.totalHours || 0,
        '"' + startStr + '"',
        '"' + endStr + '"',
        op.isUnscheduled ? 'TRUE' : 'FALSE'
      ];
    });

    return [headers.join(','), ...rows.map(r => r.join(','))].join('\n');
  }

  return {
    DAYS,
    DAY_LABELS,
    WORK_START_HOUR,
    WORK_END_HOUR,
    HOURS_PER_DAY,
    SLOTS_PER_HOUR,
    SLOTS_PER_DAY,
    TOTAL_DAYS,
    TOTAL_SLOTS,
    SLOT_DURATION_HOURS,
    PRIORITIES,

    slotToTime,
    slotToTimeEnd,
    timeToSlot,
    formatSlotRange,

    getDefaultWorkshop,
    cloneState,
    validatePlanSchema,
    escapeHTML,
    sanitizeColor,
    validateSchedule,
    calculateKPIs,
    proposeSchedule,
    moveOperation,
    autoResolveConflicts,
    applyUrgentOrderScenario,
    applyExtendedOutageScenario,
    generateMarkdownExport,
    generateCSVExport
  };
});
