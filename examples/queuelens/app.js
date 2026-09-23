/**
 * QueueLens - Core Application Logic
 * Self-contained local support-request backlog explorer.
 *
 * Guaranteed local processing: zero external network calls.
 * Preserves usable data while explaining malformed/excluded records.
 */

(function () {
  'use strict';

  // --- Constants & Defaults ---
  const PRIORITIES = ['Urgent', 'High', 'Medium', 'Low'];
  const PRIORITY_RANKS = { Urgent: 4, High: 3, Medium: 2, Low: 1, Normal: 2 };
  const STATUSES = ['Open', 'In Progress', 'Pending Customer', 'Resolved', 'Closed'];

  // --- State Store ---
  const state = {
    records: [],          // Clean and recovered usable records
    filteredRecords: [],  // Currently visible records based on search & filters
    selectedIds: new Set(),// Checkbox selections for batch export
    diagnostics: {
      totalParsed: 0,
      usableCount: 0,
      warningCount: 0,
      excludedCount: 0,
      warnings: [],       // [{ row, id, message, field, appliedDefault }]
      excluded: []        // [{ row, reason, rawData, severity }]
    },
    duplicateGroups: new Map(), // groupId -> [records]
    filters: {
      search: '',
      priority: 'ALL',
      status: 'ALL',
      assignee: 'ALL',
      duplicate: 'ALL',   // 'ALL', 'DUPLICATES_ONLY', 'PRIMARY_ONLY', 'UNIQUE_ONLY'
      tag: 'ALL'
    },
    sort: {
      field: 'created_at',
      direction: 'desc'   // 'asc' | 'desc'
    },
    currentView: 'table', // 'table' | 'kanban'
    theme: localStorage.getItem('queuelens_theme') || 'light'
  };

  // =========================================================================
  // 1. CSV & JSON Ingestion Engine (Local Parsing & Resilient Normalization)
  // =========================================================================

  class CSVParser {
    /**
     * Parses RFC 4180 CSV with automatic delimiter detection,
     * quote escaping, multiline value handling, and robust recovery.
     */
    static parse(csvText) {
      if (!csvText || typeof csvText !== 'string') {
        return { rawRows: [], errors: [{ row: 0, reason: 'Empty or invalid file content', rawData: '' }], delimiter: ',' };
      }

      // Detect delimiter from the first few non-empty lines
      const delimiter = this.detectDelimiter(csvText);
      const errors = [];
      const rawRows = [];

      let cursor = 0;
      const len = csvText.length;
      let currentRow = [];
      let currentField = '';
      let inQuotes = false;
      let quoteChar = '"';
      let rowStartLine = 1;
      let currentLine = 1;

      while (cursor < len) {
        const char = csvText[cursor];
        const nextChar = cursor + 1 < len ? csvText[cursor + 1] : '';

        if (char === '\n') {
          currentLine++;
        }

        if (inQuotes) {
          if (char === quoteChar) {
            if (nextChar === quoteChar) {
              // Escaped quote: "" -> "
              currentField += quoteChar;
              cursor += 2;
              continue;
            } else {
              // Closing quote
              inQuotes = false;
              cursor++;
              continue;
            }
          } else {
            currentField += char;
            cursor++;
            continue;
          }
        } else {
          if (char === quoteChar) {
            inQuotes = true;
            cursor++;
            continue;
          } else if (char === delimiter) {
            currentRow.push(currentField);
            currentField = '';
            cursor++;
            continue;
          } else if (char === '\r') {
            // Handle CRLF or standalone CR
            if (nextChar === '\n') {
              cursor++;
            }
            currentRow.push(currentField);
            currentField = '';
            rawRows.push({ line: rowStartLine, fields: currentRow });
            currentRow = [];
            rowStartLine = currentLine + 1;
            cursor++;
            continue;
          } else if (char === '\n') {
            currentRow.push(currentField);
            currentField = '';
            rawRows.push({ line: rowStartLine, fields: currentRow });
            currentRow = [];
            rowStartLine = currentLine;
            cursor++;
            continue;
          } else {
            currentField += char;
            cursor++;
            continue;
          }
        }
      }

      // Handle unclosed quote at end of file
      if (inQuotes) {
        errors.push({
          row: rowStartLine,
          reason: 'Unclosed quotation mark detected before end of input; row content was recovered with best-effort parsing',
          rawData: currentField.slice(0, 120),
          severity: 'warning'
        });
      }

      // Final field and row
      if (currentField !== '' || currentRow.length > 0) {
        currentRow.push(currentField);
        rawRows.push({ line: rowStartLine, fields: currentRow });
      }

      return { rawRows, errors, delimiter };
    }

    static detectDelimiter(text) {
      const candidates = [',', ';', '\t', '|'];
      const firstLines = text.split(/\r?\n/).slice(0, 5).filter(l => l.trim().length > 0);
      if (firstLines.length === 0) return ',';

      let bestCandidate = ',';
      let maxCount = -1;

      for (const cand of candidates) {
        const counts = firstLines.map(line => {
          let count = 0;
          let inQ = false;
          for (let i = 0; i < line.length; i++) {
            if (line[i] === '"') inQ = !inQ;
            if (!inQ && line[i] === cand) count++;
          }
          return count;
        });
        const avg = counts.reduce((a, b) => a + b, 0) / counts.length;
        if (avg > maxCount && avg > 0) {
          maxCount = avg;
          bestCandidate = cand;
        }
      }
      return bestCandidate;
    }
  }

  class RecordNormalizer {
    static ALIAS_MAP = {
      id: ['id', 'ticket_id', 'ticketid', 'request_id', 'issue_id', 'ticket_number', 'number', 'ref', 'key'],
      title: ['title', 'subject', 'summary', 'headline', 'issue', 'name', 'topic'],
      description: ['description', 'details', 'body', 'message', 'notes', 'content', 'text', 'narrative'],
      priority: ['priority', 'urgency', 'severity', 'level', 'prio', 'importance'],
      status: ['status', 'state', 'stage', 'phase', 'resolution_status'],
      assignee: ['assignee', 'assigned_to', 'assignedto', 'owner', 'agent', 'handler', 'engineer'],
      customer: ['customer', 'requester', 'client', 'company', 'user', 'submitter', 'contact', 'email'],
      created_at: ['created_at', 'created', 'date', 'timestamp', 'opened_at', 'created_date', 'datetime'],
      tags: ['tags', 'labels', 'categories', 'keywords'],
      channel: ['channel', 'source', 'origin', 'medium', 'entry_point']
    };

    /**
     * Normalizes CSV raw rows into standard Ticket objects,
     * classifying clean, recovered (warning), and excluded records.
     */
    static normalizeCSV(rawRows, parseErrors = []) {
      const records = [];
      const warnings = [];
      const excluded = [];

      // Forward syntax/parser errors
      for (const err of parseErrors) {
        if (err.severity === 'warning') {
          warnings.push({ row: err.row, id: 'SYNTAX', message: err.reason, field: 'csv_syntax', appliedDefault: 'Recovered' });
        } else {
          excluded.push(err);
        }
      }

      if (!rawRows || rawRows.length === 0) {
        return { records, warnings, excluded, totalParsed: 0 };
      }

      // First non-empty row is header
      let headerRowIndex = 0;
      while (headerRowIndex < rawRows.length && rawRows[headerRowIndex].fields.every(f => !f.trim())) {
        excluded.push({
          row: rawRows[headerRowIndex].line,
          reason: 'Empty row preceding table header',
          rawData: '',
          severity: 'excluded'
        });
        headerRowIndex++;
      }

      if (headerRowIndex >= rawRows.length) {
        return { records, warnings, excluded, totalParsed: rawRows.length };
      }

      const headerLine = rawRows[headerRowIndex];
      const rawHeaders = headerLine.fields.map(h => h.trim());
      const columnMapping = this.mapHeaders(rawHeaders);

      let totalParsed = 0;

      for (let i = headerRowIndex + 1; i < rawRows.length; i++) {
        totalParsed++;
        const rowItem = rawRows[i];
        const fields = rowItem.fields;
        const lineNum = rowItem.line;

        // Check if completely empty row
        if (fields.length === 0 || fields.every(f => !f || !f.trim())) {
          excluded.push({
            row: lineNum,
            reason: 'Blank or empty row containing no values',
            rawData: fields.join(','),
            severity: 'excluded'
          });
          continue;
        }

        // Build raw object from headers
        const rawObj = {};
        for (let colIdx = 0; colIdx < rawHeaders.length; colIdx++) {
          const headerName = rawHeaders[colIdx] || `Column_${colIdx + 1}`;
          rawObj[headerName] = colIdx < fields.length ? fields[colIdx] : '';
        }

        // Handle extra columns if row has more fields than header
        if (fields.length > rawHeaders.length) {
          const extraVals = fields.slice(rawHeaders.length);
          rawObj['_extra_unnamed_columns'] = extraVals.join(' | ');
          warnings.push({
            row: lineNum,
            id: rawObj[rawHeaders[0]] || `Row_${lineNum}`,
            message: `Row contains ${fields.length} columns, exceeding header count of ${rawHeaders.length}; excess values preserved in extra fields`,
            field: 'columns',
            appliedDefault: 'Preserved'
          });
        }

        const normalized = this.processRecordObject(rawObj, lineNum, columnMapping);
        if (normalized.excluded) {
          excluded.push(normalized.excluded);
        } else {
          records.push(normalized.record);
          if (normalized.warnings && normalized.warnings.length > 0) {
            warnings.push(...normalized.warnings);
          }
        }
      }

      return { records, warnings, excluded, totalParsed };
    }

    /**
     * Normalizes JSON input (array or wrapped object).
     */
    static normalizeJSON(jsonText) {
      const records = [];
      const warnings = [];
      const excluded = [];

      let parsedData;
      try {
        parsedData = JSON.parse(jsonText);
      } catch (err) {
        return {
          records: [],
          warnings: [],
          excluded: [{
            row: 1,
            reason: `Invalid JSON syntax: ${err.message}`,
            rawData: jsonText.slice(0, 150),
            severity: 'excluded'
          }],
          totalParsed: 0
        };
      }

      // Handle wrapped arrays like { "tickets": [...] } or { "requests": [...] }
      let items = [];
      if (Array.isArray(parsedData)) {
        items = parsedData;
      } else if (parsedData && typeof parsedData === 'object') {
        const potentialKeys = ['tickets', 'requests', 'data', 'items', 'rows', 'backlog'];
        for (const k of potentialKeys) {
          if (Array.isArray(parsedData[k])) {
            items = parsedData[k];
            break;
          }
        }
        if (items.length === 0) {
          // If single ticket object passed
          if (parsedData.title || parsedData.id || parsedData.subject) {
            items = [parsedData];
          } else {
            return {
              records: [],
              warnings: [],
              excluded: [{
                row: 1,
                reason: 'Root JSON object does not contain an array of support requests or recognizably structured tickets',
                rawData: JSON.stringify(parsedData).slice(0, 150),
                severity: 'excluded'
              }],
              totalParsed: 1
            };
          }
        }
      } else {
        return {
          records: [],
          warnings: [],
          excluded: [{
            row: 1,
            reason: 'Expected a JSON Array or Object containing tickets',
            rawData: String(parsedData).slice(0, 100),
            severity: 'excluded'
          }],
          totalParsed: 0
        };
      }

      let totalParsed = 0;
      for (let idx = 0; idx < items.length; idx++) {
        totalParsed++;
        const item = items[idx];
        const rowNum = idx + 1;

        if (!item || typeof item !== 'object' || Array.isArray(item)) {
          excluded.push({
            row: rowNum,
            reason: 'Item is not a valid JSON record object (null or primitive)',
            rawData: String(item),
            severity: 'excluded'
          });
          continue;
        }

        if (Object.keys(item).length === 0) {
          excluded.push({
            row: rowNum,
            reason: 'Empty JSON object with no properties',
            rawData: '{}',
            severity: 'excluded'
          });
          continue;
        }

        const columnMapping = this.mapHeaders(Object.keys(item));
        const normalized = this.processRecordObject(item, rowNum, columnMapping);

        if (normalized.excluded) {
          excluded.push(normalized.excluded);
        } else {
          records.push(normalized.record);
          if (normalized.warnings && normalized.warnings.length > 0) {
            warnings.push(...normalized.warnings);
          }
        }
      }

      return { records, warnings, excluded, totalParsed };
    }

    static mapHeaders(headers) {
      const mapping = {};
      for (const h of headers) {
        const clean = h.trim().toLowerCase().replace(/[\s\-_]+/g, '_');
        let matched = false;
        for (const [canonical, aliases] of Object.entries(this.ALIAS_MAP)) {
          if (aliases.includes(clean)) {
            mapping[canonical] = h;
            matched = true;
            break;
          }
        }
        if (!matched) {
          // Keep as custom field
        }
      }
      return mapping;
    }

    static processRecordObject(rawObj, rowNum, columnMapping) {
      const warnings = [];

      // Extract raw values based on mapping or direct match
      const getValue = (canonicalKey) => {
        const headerName = columnMapping[canonicalKey];
        if (headerName && rawObj[headerName] !== undefined) {
          return rawObj[headerName];
        }
        // Direct case-insensitive fallback
        for (const [k, v] of Object.entries(rawObj)) {
          if (k.trim().toLowerCase() === canonicalKey) return v;
        }
        return undefined;
      };

      let id = getValue('id');
      let title = getValue('title');
      let description = getValue('description');
      let priority = getValue('priority');
      let status = getValue('status');
      let assignee = getValue('assignee');
      let customer = getValue('customer');
      let createdAt = getValue('created_at');
      let tagsVal = getValue('tags');
      let channel = getValue('channel');

      // Check for completely unidentifiable records
      const hasId = id !== undefined && String(id).trim().length > 0;
      const hasTitle = title !== undefined && String(title).trim().length > 0;
      const hasDesc = description !== undefined && String(description).trim().length > 0;

      if (!hasId && !hasTitle && !hasDesc) {
        return {
          excluded: {
            row: rowNum,
            reason: 'Record lacks identifier, title, and description; insufficient content to explore or track',
            rawData: JSON.stringify(rawObj).slice(0, 150),
            severity: 'excluded'
          }
        };
      }

      // Safe recovery for ID: auto-generate synthetic ID rather than dropping usable record
      if (!hasId) {
        id = `AUTO-${String(rowNum).padStart(4, '0')}`;
        warnings.push({
          row: rowNum,
          id: id,
          message: 'Missing ticket ID; auto-assigned synthetic ID to preserve usable data',
          field: 'id',
          appliedDefault: id
        });
      } else {
        id = String(id).trim();
      }

      // Safe recovery for Title: synthesize from description snippet
      if (!hasTitle) {
        const snippet = hasDesc ? String(description).trim().slice(0, 60) : 'Untitled Support Request';
        title = `[Untitled] ${snippet}...`;
        warnings.push({
          row: rowNum,
          id: id,
          message: 'Missing title/subject; synthesized from description preview',
          field: 'title',
          appliedDefault: title
        });
      } else {
        title = String(title).trim();
      }

      // Safe recovery for Description
      description = description !== undefined ? String(description).trim() : '';

      // Safe recovery for Priority: normalize and default to Normal / Medium
      let normalizedPriority = 'Medium';
      let preservePriorityVal = undefined;
      if (priority !== undefined && priority !== null && String(priority).trim().length > 0) {
        const pStr = String(priority).trim().toLowerCase();
        if (pStr.includes('urg') || pStr.includes('crit') || pStr === 'p1') {
          normalizedPriority = 'Urgent';
        } else if (pStr.includes('high') || pStr === 'p2') {
          normalizedPriority = 'High';
        } else if (pStr.includes('med') || pStr.includes('norm') || pStr === 'p3') {
          normalizedPriority = 'Medium';
        } else if (pStr.includes('low') || pStr.includes('min') || pStr === 'p4') {
          normalizedPriority = 'Low';
        } else {
          normalizedPriority = 'Medium';
          preservePriorityVal = priority;
          warnings.push({
            row: rowNum,
            id: id,
            message: `Unrecognized priority "${priority}"; defaulted to Medium (preserved in custom_fields)`,
            field: 'priority',
            appliedDefault: 'Medium'
          });
        }
      } else if (priority !== undefined) {
        // Explicitly passed null or empty value
        normalizedPriority = 'Medium';
        preservePriorityVal = priority;
        warnings.push({
          row: rowNum,
          id: id,
          message: `Unrecognized priority "${priority}"; defaulted to Medium (preserved in custom_fields)`,
          field: 'priority',
          appliedDefault: 'Medium'
        });
      } else {
        warnings.push({
          row: rowNum,
          id: id,
          message: 'Missing priority field; defaulted to Medium',
          field: 'priority',
          appliedDefault: 'Medium'
        });
      }

      // Safe recovery for Status
      let normalizedStatus = 'Open';
      let preserveStatusVal = undefined;
      if (status !== undefined && status !== null && String(status).trim().length > 0) {
        const sStr = String(status).trim().toLowerCase();
        if (sStr.includes('prog') || sStr.includes('work') || sStr.includes('dev')) {
          normalizedStatus = 'In Progress';
        } else if (sStr.includes('pend') || sStr.includes('wait') || sStr.includes('hold')) {
          normalizedStatus = 'Pending Customer';
        } else if (sStr.includes('resolv') || sStr.includes('done') || sStr.includes('fixed')) {
          normalizedStatus = 'Resolved';
        } else if (sStr.includes('clos')) {
          normalizedStatus = 'Closed';
        } else if (sStr.includes('open') || sStr.includes('new')) {
          normalizedStatus = 'Open';
        } else {
          normalizedStatus = 'Open';
          preserveStatusVal = status;
          warnings.push({
            row: rowNum,
            id: id,
            message: `Unrecognized status "${status}"; defaulted to Open (preserved in custom_fields)`,
            field: 'status',
            appliedDefault: 'Open'
          });
        }
      } else if (status !== undefined) {
        // Explicitly passed null or empty value
        normalizedStatus = 'Open';
        preserveStatusVal = status;
        warnings.push({
          row: rowNum,
          id: id,
          message: `Unrecognized status "${status}"; defaulted to Open (preserved in custom_fields)`,
          field: 'status',
          appliedDefault: 'Open'
        });
      } else {
        warnings.push({
          row: rowNum,
          id: id,
          message: 'Missing status field; defaulted to Open',
          field: 'status',
          appliedDefault: 'Open'
        });
      }

      // Safe recovery for Assignee: preserve empty as Unassigned
      assignee = assignee !== undefined ? String(assignee).trim() : '';

      // Customer
      customer = customer !== undefined ? String(customer).trim() : 'Unknown Customer';

      // Date normalization
      let formattedDate = new Date().toISOString();
      let rawDateString = '';
      if (createdAt !== undefined && String(createdAt).trim().length > 0) {
        rawDateString = String(createdAt).trim();
        const parsedTimestamp = Date.parse(rawDateString);
        if (!isNaN(parsedTimestamp)) {
          formattedDate = new Date(parsedTimestamp).toISOString();
        } else {
          warnings.push({
            row: rowNum,
            id: id,
            message: `Date "${rawDateString}" could not be parsed into ISO timestamp; current time assigned, raw preserved`,
            field: 'created_at',
            appliedDefault: formattedDate
          });
        }
      } else {
        rawDateString = formattedDate;
      }

      // Tags normalization: array of strings
      let tags = [];
      if (tagsVal) {
        if (Array.isArray(tagsVal)) {
          tags = tagsVal.map(t => String(t).trim()).filter(Boolean);
        } else if (typeof tagsVal === 'string') {
          tags = tagsVal.split(/[,;|]/).map(t => t.trim()).filter(Boolean);
        }
      }

      // Channel
      channel = channel !== undefined ? String(channel).trim() : 'Direct';

      // Preserve all custom / extra fields without losing data
      const customFields = {};
      const standardKeys = new Set(Object.values(columnMapping).map(k => String(k).toLowerCase()));
      for (const [k, v] of Object.entries(rawObj)) {
        if (!standardKeys.has(k.trim().toLowerCase())) {
          customFields[k] = v;
        }
      }

      // Preserve unmapped/unrecognized priority or status in custom_fields as promised
      if (preservePriorityVal !== undefined) {
        const pKey = columnMapping['priority'] || 'priority';
        customFields[pKey] = preservePriorityVal;
        customFields['raw_priority'] = preservePriorityVal;
      }
      if (preserveStatusVal !== undefined) {
        const sKey = columnMapping['status'] || 'status';
        customFields[sKey] = preserveStatusVal;
        customFields['raw_status'] = preserveStatusVal;
      }

      const record = {
        id,
        title,
        description,
        priority: normalizedPriority,
        status: normalizedStatus,
        assignee,
        customer,
        created_at: formattedDate,
        raw_created_at: rawDateString,
        tags,
        channel,
        custom_fields: customFields,
        row_number: rowNum,
        // Duplicate detection fields (populated by DuplicateDetector)
        is_duplicate: false,
        duplicate_group_id: null,
        duplicate_role: 'unique', // 'primary' | 'duplicate' | 'unique'
        duplicate_of: null,
        duplicate_similarity: 0
      };

      return { record, warnings };
    }
  }

  // =========================================================================
  // 2. Duplicate Detection Engine
  // =========================================================================

  class DuplicateDetector {
    /**
     * Identifies duplicate tickets using multi-pass matching:
     * 1. Exact match on normalized title + customer
     * 2. High similarity on normalized title + issue tokens
     * Groups related duplicates and marks the earliest as 'primary'.
     */
    static analyze(records) {
      // Reset duplicate metadata
      for (const r of records) {
        r.is_duplicate = false;
        r.duplicate_group_id = null;
        r.duplicate_role = 'unique';
        r.duplicate_of = null;
        r.duplicate_similarity = 0;
      }

      const groups = new Map(); // groupId -> array of records
      let groupCounter = 1;

      // Disjoint Set Union (DSU) / Union-Find for clustering
      const parent = new Map();
      const find = (i) => {
        if (!parent.has(i)) parent.set(i, i);
        if (parent.get(i) !== i) {
          parent.set(i, find(parent.get(i)));
        }
        return parent.get(i);
      };
      const union = (i, j) => {
        const rootI = find(i);
        const rootJ = find(j);
        if (rootI !== rootJ) parent.set(rootI, rootJ);
      };

      // Helper: normalize text for comparison
      const normalizeText = (str) => {
        if (!str) return '';
        return str
          .toLowerCase()
          .replace(/^(re:|fwd:|ticket:|issue:|\s+)+/gi, '')
          .replace(/[^a-z0-9\s]/g, ' ')
          .replace(/\s+/g, ' ')
          .trim();
      };

      // Helper: compute token Jaccard similarity
      const tokenSimilarity = (textA, textB) => {
        const tokensA = new Set(textA.split(' ').filter(w => w.length > 2));
        const tokensB = new Set(textB.split(' ').filter(w => w.length > 2));
        if (tokensA.size === 0 || tokensB.size === 0) return 0;

        let intersection = 0;
        for (const t of tokensA) {
          if (tokensB.has(t)) intersection++;
        }
        const unionSize = tokensA.size + tokensB.size - intersection;
        return unionSize === 0 ? 0 : intersection / unionSize;
      };

      const normalizedRecords = records.map((rec, idx) => ({
        index: idx,
        record: rec,
        normTitle: normalizeText(rec.title),
        normCustomer: normalizeText(rec.customer),
        normDesc: normalizeText(rec.description.slice(0, 150))
      }));

      // Compare pairs for duplicates
      for (let i = 0; i < normalizedRecords.length; i++) {
        for (let j = i + 1; j < normalizedRecords.length; j++) {
          const itemA = normalizedRecords[i];
          const itemB = normalizedRecords[j];

          // Condition 1: Exact title match
          const exactTitle = itemA.normTitle.length > 5 && itemA.normTitle === itemB.normTitle;
          const sameCustomer = itemA.normCustomer.length > 2 && itemA.normCustomer === itemB.normCustomer;

          // Condition 2: High token similarity (>= 80%) on title
          const titleSim = tokenSimilarity(itemA.normTitle, itemB.normTitle);

          // Condition 3: Exact description snippet match
          const exactDesc = itemA.normDesc.length > 20 && itemA.normDesc === itemB.normDesc;

          let isMatch = false;
          let matchScore = 0;

          if (exactTitle) {
            isMatch = true;
            matchScore = 1.0;
          } else if (titleSim >= 0.8) {
            isMatch = true;
            matchScore = titleSim;
          } else if (exactDesc && sameCustomer) {
            isMatch = true;
            matchScore = 0.95;
          }

          if (isMatch) {
            union(i, j);
            // Record similarity hint
            itemB.record.duplicate_similarity = Math.max(itemB.record.duplicate_similarity, matchScore);
            itemA.record.duplicate_similarity = Math.max(itemA.record.duplicate_similarity, matchScore);
          }
        }
      }

      // Group by root cluster
      const clusters = new Map();
      for (let i = 0; i < normalizedRecords.length; i++) {
        const root = find(i);
        if (!clusters.has(root)) clusters.set(root, []);
        clusters.get(root).push(normalizedRecords[i].record);
      }

      // Label duplicate clusters with > 1 item
      for (const [, clusterMembers] of clusters) {
        if (clusterMembers.length > 1) {
          const groupId = `DUP-${String(groupCounter++).padStart(2, '0')}`;

          // Sort by creation date or row number to identify Primary (earliest)
          clusterMembers.sort((a, b) => {
            const dateA = Date.parse(a.created_at) || 0;
            const dateB = Date.parse(b.created_at) || 0;
            return dateA - dateB || a.row_number - b.row_number;
          });

          const primary = clusterMembers[0];
          primary.is_duplicate = true;
          primary.duplicate_group_id = groupId;
          primary.duplicate_role = 'primary';
          primary.duplicate_of = primary.id;

          for (let k = 1; k < clusterMembers.length; k++) {
            const dup = clusterMembers[k];
            dup.is_duplicate = true;
            dup.duplicate_group_id = groupId;
            dup.duplicate_role = 'duplicate';
            dup.duplicate_of = primary.id;
          }

          groups.set(groupId, clusterMembers);
        }
      }

      return groups;
    }
  }

  // =========================================================================
  // 3. Search & Filter Engine
  // =========================================================================

  class FilterEngine {
    static apply(records, filters, sort) {
      let results = [...records];

      // 1. Search filter (text across title, description, id, customer, tags, assignee)
      if (filters.search && filters.search.trim()) {
        const query = filters.search.trim().toLowerCase();
        results = results.filter(r => {
          const matchId = r.id.toLowerCase().includes(query);
          const matchTitle = r.title.toLowerCase().includes(query);
          const matchDesc = r.description.toLowerCase().includes(query);
          const matchCust = r.customer.toLowerCase().includes(query);
          const matchAssn = (r.assignee || 'unassigned').toLowerCase().includes(query);
          const matchTags = r.tags.some(t => t.toLowerCase().includes(query));
          const matchChannel = (r.channel || '').toLowerCase().includes(query);
          return matchId || matchTitle || matchDesc || matchCust || matchAssn || matchTags || matchChannel;
        });
      }

      // 2. Priority filter
      if (filters.priority && filters.priority !== 'ALL') {
        results = results.filter(r => r.priority.toLowerCase() === filters.priority.toLowerCase());
      }

      // 3. Status filter
      if (filters.status && filters.status !== 'ALL') {
        results = results.filter(r => r.status.toLowerCase() === filters.status.toLowerCase());
      }

      // 4. Assignee filter
      if (filters.assignee && filters.assignee !== 'ALL') {
        if (filters.assignee === '__UNASSIGNED__') {
          results = results.filter(r => !r.assignee || r.assignee.trim().length === 0);
        } else {
          results = results.filter(r => r.assignee === filters.assignee);
        }
      }

      // 5. Duplicate filter
      if (filters.duplicate && filters.duplicate !== 'ALL') {
        if (filters.duplicate === 'DUPLICATES_ONLY') {
          results = results.filter(r => r.is_duplicate);
        } else if (filters.duplicate === 'PRIMARY_ONLY') {
          results = results.filter(r => r.duplicate_role === 'primary');
        } else if (filters.duplicate === 'UNIQUE_ONLY') {
          results = results.filter(r => !r.is_duplicate);
        }
      }

      // 6. Tag filter
      if (filters.tag && filters.tag !== 'ALL') {
        results = results.filter(r => r.tags.includes(filters.tag));
      }

      // 7. Sorting
      results.sort((a, b) => {
        let comparison = 0;
        if (sort.field === 'priority') {
          const rankA = PRIORITY_RANKS[a.priority] || 0;
          const rankB = PRIORITY_RANKS[b.priority] || 0;
          comparison = rankB - rankA; // default High first
          if (sort.direction === 'asc') comparison = -comparison;
        } else if (sort.field === 'created_at') {
          const timeA = Date.parse(a.created_at) || 0;
          const timeB = Date.parse(b.created_at) || 0;
          comparison = timeA - timeB;
          if (sort.direction === 'desc') comparison = -comparison;
        } else if (sort.field === 'id') {
          comparison = a.id.localeCompare(b.id, undefined, { numeric: true });
          if (sort.direction === 'desc') comparison = -comparison;
        } else if (sort.field === 'title') {
          comparison = a.title.localeCompare(b.title);
          if (sort.direction === 'desc') comparison = -comparison;
        } else if (sort.field === 'status') {
          comparison = a.status.localeCompare(b.status);
          if (sort.direction === 'desc') comparison = -comparison;
        }
        return comparison || (a.row_number - b.row_number);
      });

      return results;
    }
  }

  // =========================================================================
  // 4. Export Engine (CSV & JSON format generator)
  // =========================================================================

  class ExportEngine {
    static toCSV(records) {
      if (!records || records.length === 0) return '';

      // Collect standard and common custom headers
      const baseHeaders = ['id', 'title', 'description', 'priority', 'status', 'assignee', 'customer', 'created_at', 'tags', 'channel', 'is_duplicate', 'duplicate_group_id', 'duplicate_role', 'duplicate_of'];
      const customHeaderSet = new Set();
      for (const r of records) {
        if (r.custom_fields) {
          for (const k of Object.keys(r.custom_fields)) {
            if (!baseHeaders.includes(k)) {
              customHeaderSet.add(k);
            }
          }
        }
      }
      const allHeaders = [...baseHeaders, ...Array.from(customHeaderSet)];

      const escapeCSV = (val) => {
        if (val === null || val === undefined) return '""';
        let str = String(val);
        if (Array.isArray(val)) str = val.join('; ');

        // SEC-002: Neutralize spreadsheet formula injection (=, +, -, @, \t, \r)
        // Prefix formula-initiating characters with a single quote (') so spreadsheet
        // applications treat cell values strictly as literal text.
        if (/^[\s]*[=+\-@\t\r]/.test(str)) {
          str = "'" + str;
        }

        // If string contains comma, quote, or newline, escape quotes and wrap in quotes
        if (str.includes('"') || str.includes(',') || str.includes('\n') || str.includes('\r')) {
          return `"${str.replace(/"/g, '""')}"`;
        }
        return `"${str}"`;
      };

      const rows = [allHeaders.join(',')];

      for (const r of records) {
        const rowVals = allHeaders.map(h => {
          if (h in r) {
            return escapeCSV(r[h]);
          } else if (r.custom_fields && h in r.custom_fields) {
            return escapeCSV(r.custom_fields[h]);
          }
          return '""';
        });
        rows.push(rowVals.join(','));
      }

      return rows.join('\r\n');
    }

    static toJSON(records) {
      return JSON.stringify(records, null, 2);
    }

    static downloadFile(filename, content, mimeType) {
      const blob = new Blob([content], { type: `${mimeType};charset=utf-8;` });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.setAttribute('href', url);
      link.setAttribute('download', filename);
      link.style.visibility = 'hidden';
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    }
  }

  // =========================================================================
  // 5. UI Controller & View Renderer
  // =========================================================================

  class UIController {
    static init() {
      if (typeof document === 'undefined' || !document.getElementById || !document.getElementById('theme-toggle-btn')) {
        return;
      }
      this.bindElements();
      this.attachEventListeners();
      this.applyTheme(state.theme);

      // Auto-load standard sample data so user immediately sees rich data
      if (window.QUEUELENS_SAMPLES && window.QUEUELENS_SAMPLES.standard) {
        this.loadSample('standard');
      }
    }

    static bindElements() {
      this.dom = {
        // App header & controls
        themeToggleBtn: document.getElementById('theme-toggle-btn'),
        sampleStandardBtn: document.getElementById('sample-standard-btn'),
        sampleIssuesBtn: document.getElementById('sample-issues-btn'),
        importBtn: document.getElementById('import-btn'),
        fileInput: document.getElementById('file-input'),
        exportDropdownBtn: document.getElementById('export-dropdown-btn'),
        exportMenu: document.getElementById('export-menu'),
        exportFilteredCsvBtn: document.getElementById('export-filtered-csv'),
        exportFilteredJsonBtn: document.getElementById('export-filtered-json'),
        exportSelectedCsvBtn: document.getElementById('export-selected-csv'),
        exportSelectedJsonBtn: document.getElementById('export-selected-json'),
        diagnosticsBadgeBtn: document.getElementById('diagnostics-badge-btn'),
        duplicatesBadgeBtn: document.getElementById('duplicates-badge-btn'),

        // Stats summary strip
        statTotal: document.getElementById('stat-total'),
        statFiltered: document.getElementById('stat-filtered'),
        statUrgent: document.getElementById('stat-urgent'),
        statOpen: document.getElementById('stat-open'),
        statUnassigned: document.getElementById('stat-unassigned'),
        statDuplicates: document.getElementById('stat-duplicates'),
        statHealthBadge: document.getElementById('stat-health-badge'),

        // Filters
        searchInput: document.getElementById('search-input'),
        clearSearchBtn: document.getElementById('clear-search-btn'),
        priorityFilter: document.getElementById('priority-filter'),
        statusFilter: document.getElementById('status-filter'),
        assigneeFilter: document.getElementById('assignee-filter'),
        duplicateFilter: document.getElementById('duplicate-filter'),
        sortField: document.getElementById('sort-field'),
        sortDirectionBtn: document.getElementById('sort-direction-btn'),
        resetFiltersBtn: document.getElementById('reset-filters-btn'),
        activeFilterChips: document.getElementById('active-filter-chips'),

        // Selection & Views
        selectAllCheckbox: document.getElementById('select-all-checkbox'),
        selectionCountText: document.getElementById('selection-count-text'),
        tableViewBtn: document.getElementById('table-view-btn'),
        kanbanViewBtn: document.getElementById('kanban-view-btn'),
        tableViewContainer: document.getElementById('table-view-container'),
        kanbanViewContainer: document.getElementById('kanban-view-container'),
        tableBody: document.getElementById('tickets-table-body'),
        emptyState: document.getElementById('empty-state'),

        // Modals
        ticketModal: document.getElementById('ticket-modal'),
        ticketModalClose: document.getElementById('ticket-modal-close'),
        ticketModalContent: document.getElementById('ticket-modal-content'),

        diagnosticsModal: document.getElementById('diagnostics-modal'),
        diagnosticsModalClose: document.getElementById('diagnostics-modal-close'),
        diagnosticsModalBody: document.getElementById('diagnostics-modal-body'),
        exportDiagnosticsBtn: document.getElementById('export-diagnostics-btn'),

        duplicateModal: document.getElementById('duplicate-modal'),
        duplicateModalClose: document.getElementById('duplicate-modal-close'),
        duplicateModalBody: document.getElementById('duplicate-modal-body'),

        importModal: document.getElementById('import-modal'),
        importModalClose: document.getElementById('import-modal-close'),
        dropZone: document.getElementById('drop-zone'),
        rawPasteInput: document.getElementById('raw-paste-input'),
        processPasteBtn: document.getElementById('process-paste-btn'),

        toastContainer: document.getElementById('toast-container')
      };
    }

    static attachEventListeners() {
      // Theme toggle
      this.dom.themeToggleBtn.addEventListener('click', () => {
        const nextTheme = state.theme === 'dark' ? 'light' : 'dark';
        this.applyTheme(nextTheme);
      });

      // Sample Data buttons
      this.dom.sampleStandardBtn.addEventListener('click', () => this.loadSample('standard'));
      this.dom.sampleIssuesBtn.addEventListener('click', () => this.loadSample('issues'));

      // File Import triggers
      this.dom.importBtn.addEventListener('click', () => this.openImportModal());
      this.dom.importModalClose.addEventListener('click', () => this.closeImportModal());
      this.dom.fileInput.addEventListener('change', (e) => this.handleFileSelect(e));

      // Drag & Drop
      ['dragenter', 'dragover'].forEach(name => {
        this.dom.dropZone.addEventListener(name, (e) => {
          e.preventDefault();
          this.dom.dropZone.classList.add('drag-active');
        });
      });
      ['dragleave', 'drop'].forEach(name => {
        this.dom.dropZone.addEventListener(name, (e) => {
          e.preventDefault();
          this.dom.dropZone.classList.remove('drag-active');
        });
      });
      this.dom.dropZone.addEventListener('drop', (e) => {
        const files = e.dataTransfer.files;
        if (files.length > 0) this.processFile(files[0]);
      });
      this.dom.dropZone.addEventListener('click', () => this.dom.fileInput.click());

      // Raw text paste
      this.dom.processPasteBtn.addEventListener('click', () => {
        const text = this.dom.rawPasteInput.value.trim();
        if (text) {
          this.processRawText(text, 'pasted_input');
          this.closeImportModal();
        } else {
          this.showToast('Please paste valid CSV or JSON text', 'warning');
        }
      });

      // Export Menu
      this.dom.exportDropdownBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        this.dom.exportMenu.classList.toggle('show');
      });
      document.addEventListener('click', () => {
        this.dom.exportMenu.classList.remove('show');
      });

      this.dom.exportFilteredCsvBtn.addEventListener('click', () => this.exportFiltered('csv'));
      this.dom.exportFilteredJsonBtn.addEventListener('click', () => this.exportFiltered('json'));
      this.dom.exportSelectedCsvBtn.addEventListener('click', () => this.exportSelected('csv'));
      this.dom.exportSelectedJsonBtn.addEventListener('click', () => this.exportSelected('json'));

      // Search & Filters
      this.dom.searchInput.addEventListener('input', (e) => {
        state.filters.search = e.target.value;
        this.dom.clearSearchBtn.style.display = state.filters.search ? 'inline-flex' : 'none';
        this.updateView();
      });
      this.dom.clearSearchBtn.addEventListener('click', () => {
        this.dom.searchInput.value = '';
        state.filters.search = '';
        this.dom.clearSearchBtn.style.display = 'none';
        this.updateView();
      });

      this.dom.priorityFilter.addEventListener('change', (e) => {
        state.filters.priority = e.target.value;
        this.updateView();
      });
      this.dom.statusFilter.addEventListener('change', (e) => {
        state.filters.status = e.target.value;
        this.updateView();
      });
      this.dom.assigneeFilter.addEventListener('change', (e) => {
        state.filters.assignee = e.target.value;
        this.updateView();
      });
      this.dom.duplicateFilter.addEventListener('change', (e) => {
        state.filters.duplicate = e.target.value;
        this.updateView();
      });

      this.dom.sortField.addEventListener('change', (e) => {
        state.sort.field = e.target.value;
        this.updateView();
      });
      this.dom.sortDirectionBtn.addEventListener('click', () => {
        state.sort.direction = state.sort.direction === 'asc' ? 'desc' : 'asc';
        this.dom.sortDirectionBtn.textContent = state.sort.direction === 'asc' ? '▲ Asc' : '▼ Desc';
        this.updateView();
      });

      this.dom.resetFiltersBtn.addEventListener('click', () => this.resetFilters());

      // View switcher
      this.dom.tableViewBtn.addEventListener('click', () => this.switchView('table'));
      this.dom.kanbanViewBtn.addEventListener('click', () => this.switchView('kanban'));

      // Selection
      this.dom.selectAllCheckbox.addEventListener('change', (e) => {
        const isChecked = e.target.checked;
        if (isChecked) {
          state.filteredRecords.forEach(r => state.selectedIds.add(r.id));
        } else {
          state.selectedIds.clear();
        }
        this.renderTableRows();
        this.updateSelectionStats();
      });

      // Modals
      this.dom.diagnosticsBadgeBtn.addEventListener('click', () => this.openDiagnosticsModal());
      this.dom.diagnosticsModalClose.addEventListener('click', () => this.closeDiagnosticsModal());
      this.dom.exportDiagnosticsBtn.addEventListener('click', () => this.exportDiagnostics());

      this.dom.duplicatesBadgeBtn.addEventListener('click', () => this.openDuplicatesModal());
      this.dom.duplicateModalClose.addEventListener('click', () => this.closeDuplicatesModal());

      this.dom.ticketModalClose.addEventListener('click', () => this.closeTicketModal());

      // Additional modal & empty state actions (replacing inline handlers)
      const emptyResetBtn = document.getElementById('empty-reset-filters-btn');
      if (emptyResetBtn) {
        emptyResetBtn.addEventListener('click', () => this.resetFilters());
      }
      const diagCloseFooter = document.getElementById('diagnostics-modal-close-footer');
      if (diagCloseFooter) {
        diagCloseFooter.addEventListener('click', () => this.closeDiagnosticsModal());
      }
      const dupCloseFooter = document.getElementById('duplicate-modal-close-footer');
      if (dupCloseFooter) {
        dupCloseFooter.addEventListener('click', () => this.closeDuplicatesModal());
      }

      // Close modal on Escape or backdrop click
      window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
          this.closeAllModals();
        }
      });
      [this.dom.ticketModal, this.dom.diagnosticsModal, this.dom.duplicateModal, this.dom.importModal].forEach(m => {
        m.addEventListener('click', (e) => {
          if (e.target === m) this.closeAllModals();
        });
      });
    }

    static applyTheme(theme) {
      state.theme = theme;
      localStorage.setItem('queuelens_theme', theme);
      if (theme === 'dark') {
        document.documentElement.setAttribute('data-theme', 'dark');
        this.dom.themeToggleBtn.textContent = '☀️ Light';
      } else {
        document.documentElement.removeAttribute('data-theme');
        this.dom.themeToggleBtn.textContent = '🌙 Dark';
      }
    }

    static resetFilters() {
      state.filters.search = '';
      state.filters.priority = 'ALL';
      state.filters.status = 'ALL';
      state.filters.assignee = 'ALL';
      state.filters.duplicate = 'ALL';
      state.filters.tag = 'ALL';

      this.dom.searchInput.value = '';
      this.dom.clearSearchBtn.style.display = 'none';
      this.dom.priorityFilter.value = 'ALL';
      this.dom.statusFilter.value = 'ALL';
      this.dom.assigneeFilter.value = 'ALL';
      this.dom.duplicateFilter.value = 'ALL';

      this.updateView();
      this.showToast('Filters reset', 'info');
    }

    static switchView(viewName) {
      state.currentView = viewName;
      if (viewName === 'table') {
        this.dom.tableViewBtn.classList.add('active');
        this.dom.kanbanViewBtn.classList.remove('active');
        this.dom.tableViewContainer.style.display = 'block';
        this.dom.kanbanViewContainer.style.display = 'none';
      } else {
        this.dom.tableViewBtn.classList.remove('active');
        this.dom.kanbanViewBtn.classList.add('active');
        this.dom.tableViewContainer.style.display = 'none';
        this.dom.kanbanViewContainer.style.display = 'block';
        this.renderKanban();
      }
    }

    // --- Loading & Ingestion ---

    static loadSample(sampleKey) {
      const sample = window.QUEUELENS_SAMPLES ? window.QUEUELENS_SAMPLES[sampleKey] : null;
      if (!sample) {
        this.showToast(`Sample dataset "${sampleKey}" not found`, 'error');
        return;
      }

      if (sample.format === 'json') {
        const jsonText = typeof sample.data === 'string' ? sample.data : JSON.stringify(sample.data);
        this.processRawText(jsonText, 'sample_json');
      } else if (sample.format === 'csv') {
        this.processRawText(sample.rawCsv, 'sample_csv');
      }
      this.showToast(`Loaded ${sample.name}`, 'success');
    }

    static handleFileSelect(e) {
      const file = e.target.files[0];
      if (file) {
        this.processFile(file);
        this.dom.fileInput.value = ''; // reset so same file can be reloaded
        this.closeImportModal();
      }
    }

    static processFile(file) {
      const reader = new FileReader();
      reader.onload = (event) => {
        const content = event.target.result;
        this.processRawText(content, file.name);
      };
      reader.onerror = () => {
        this.showToast(`Failed to read file "${file.name}"`, 'error');
      };
      reader.readAsText(file);
    }

    static processRawText(content, sourceName = 'file') {
      let result;
      const isJson = content.trim().startsWith('[') || content.trim().startsWith('{');

      if (isJson) {
        result = RecordNormalizer.normalizeJSON(content);
      } else {
        const { rawRows, errors } = CSVParser.parse(content);
        result = RecordNormalizer.normalizeCSV(rawRows, errors);
      }

      // Run Duplicate Detection across parsed records
      const duplicateGroups = DuplicateDetector.analyze(result.records);

      // Store in application state
      state.records = result.records;
      state.diagnostics = {
        totalParsed: result.totalParsed,
        usableCount: result.records.length,
        warningCount: result.warnings.length,
        excludedCount: result.excluded.length,
        warnings: result.warnings,
        excluded: result.excluded
      };
      state.duplicateGroups = duplicateGroups;
      state.selectedIds.clear();

      // Populate dynamic Assignee dropdown options
      this.populateAssigneeOptions();

      // Re-render view
      this.updateView();

      // Notify user
      const msg = `Imported ${result.records.length} tickets (${result.warnings.length} warnings, ${result.excluded.length} excluded)`;
      if (result.excluded.length > 0) {
        this.showToast(msg, 'warning');
      } else {
        this.showToast(msg, 'success');
      }
    }

    static populateAssigneeOptions() {
      const assignees = new Set();
      let hasUnassigned = false;

      for (const r of state.records) {
        if (r.assignee && r.assignee.trim()) {
          assignees.add(r.assignee.trim());
        } else {
          hasUnassigned = true;
        }
      }

      const select = this.dom.assigneeFilter;
      select.replaceChildren();
      const allOpt = document.createElement('option');
      allOpt.value = 'ALL';
      allOpt.textContent = 'All Assignees';
      select.appendChild(allOpt);

      if (hasUnassigned) {
        const opt = document.createElement('option');
        opt.value = '__UNASSIGNED__';
        opt.textContent = '⚠️ Unassigned';
        select.appendChild(opt);
      }

      Array.from(assignees).sort().forEach(name => {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        select.appendChild(opt);
      });
    }

    // --- View Rendering ---

    static updateView() {
      state.filteredRecords = FilterEngine.apply(state.records, state.filters, state.sort);
      this.renderStats();
      this.renderActiveChips();
      this.updateSelectionStats();

      if (state.currentView === 'table') {
        this.renderTableRows();
      } else {
        this.renderKanban();
      }
    }

    static renderStats() {
      const total = state.records.length;
      const filtered = state.filteredRecords.length;
      const urgentHigh = state.records.filter(r => r.priority === 'Urgent' || r.priority === 'High').length;
      const openProgress = state.records.filter(r => r.status === 'Open' || r.status === 'In Progress').length;
      const unassigned = state.records.filter(r => !r.assignee || !r.assignee.trim()).length;

      let duplicateCount = 0;
      for (const [, list] of state.duplicateGroups) {
        duplicateCount += list.length;
      }

      this.dom.statTotal.textContent = total;
      this.dom.statFiltered.textContent = filtered;
      this.dom.statUrgent.textContent = urgentHigh;
      this.dom.statOpen.textContent = openProgress;
      this.dom.statUnassigned.textContent = unassigned;
      this.dom.statDuplicates.textContent = duplicateCount;

      // Duplicates badge
      if (duplicateCount > 0) {
        this.dom.duplicatesBadgeBtn.style.display = 'inline-flex';
        this.dom.duplicatesBadgeBtn.textContent = `⚡ ${duplicateCount} Duplicates`;
      } else {
        this.dom.duplicatesBadgeBtn.style.display = 'none';
      }

      // Diagnostics badge
      const issuesTotal = state.diagnostics.warningCount + state.diagnostics.excludedCount;
      if (issuesTotal > 0) {
        this.dom.diagnosticsBadgeBtn.style.display = 'inline-flex';
        this.dom.diagnosticsBadgeBtn.className = state.diagnostics.excludedCount > 0 ? 'badge-btn warning' : 'badge-btn info';
        this.dom.diagnosticsBadgeBtn.textContent = `⚠️ ${issuesTotal} Data Quality Note${issuesTotal > 1 ? 's' : ''}`;
        this.dom.statHealthBadge.textContent = state.diagnostics.excludedCount > 0 ? `${state.diagnostics.excludedCount} Excluded` : `${state.diagnostics.warningCount} Warnings`;
        this.dom.statHealthBadge.className = 'stat-badge warn';
      } else {
        this.dom.diagnosticsBadgeBtn.style.display = total > 0 ? 'inline-flex' : 'none';
        this.dom.diagnosticsBadgeBtn.className = 'badge-btn success';
        this.dom.diagnosticsBadgeBtn.textContent = '✓ Clean Ingestion';
        this.dom.statHealthBadge.textContent = '100% Clean';
        this.dom.statHealthBadge.className = 'stat-badge clean';
      }
    }

    static renderActiveChips() {
      const chipsContainer = this.dom.activeFilterChips;
      chipsContainer.innerHTML = '';

      const createChip = (label, onRemove) => {
        const chip = document.createElement('span');
        chip.className = 'filter-chip';
        chip.textContent = label;
        const removeBtn = document.createElement('button');
        removeBtn.className = 'chip-remove';
        removeBtn.textContent = '×';
        removeBtn.addEventListener('click', onRemove);
        chip.appendChild(removeBtn);
        chipsContainer.appendChild(chip);
      };

      if (state.filters.search) {
        createChip(`Search: "${state.filters.search}"`, () => {
          state.filters.search = '';
          this.dom.searchInput.value = '';
          this.dom.clearSearchBtn.style.display = 'none';
          this.updateView();
        });
      }
      if (state.filters.priority !== 'ALL') {
        createChip(`Priority: ${state.filters.priority}`, () => {
          state.filters.priority = 'ALL';
          this.dom.priorityFilter.value = 'ALL';
          this.updateView();
        });
      }
      if (state.filters.status !== 'ALL') {
        createChip(`Status: ${state.filters.status}`, () => {
          state.filters.status = 'ALL';
          this.dom.statusFilter.value = 'ALL';
          this.updateView();
        });
      }
      if (state.filters.assignee !== 'ALL') {
        const label = state.filters.assignee === '__UNASSIGNED__' ? 'Unassigned' : state.filters.assignee;
        createChip(`Assignee: ${label}`, () => {
          state.filters.assignee = 'ALL';
          this.dom.assigneeFilter.value = 'ALL';
          this.updateView();
        });
      }
      if (state.filters.duplicate !== 'ALL') {
        const dupLabels = {
          'DUPLICATES_ONLY': 'Duplicates Only',
          'PRIMARY_ONLY': 'Primary Only',
          'UNIQUE_ONLY': 'Unique Only'
        };
        const label = dupLabels[state.filters.duplicate] || state.filters.duplicate;
        createChip(`Duplicates: ${label}`, () => {
          state.filters.duplicate = 'ALL';
          this.dom.duplicateFilter.value = 'ALL';
          this.updateView();
        });
      }
    }

    static renderTableRows() {
      const tbody = this.dom.tableBody;
      tbody.innerHTML = '';

      if (state.filteredRecords.length === 0) {
        this.dom.emptyState.style.display = 'block';
        this.dom.selectAllCheckbox.checked = false;
        return;
      }
      this.dom.emptyState.style.display = 'none';

      for (const rec of state.filteredRecords) {
        const tr = document.createElement('tr');
        if (rec.is_duplicate) tr.classList.add('row-duplicate');
        if (!rec.assignee) tr.classList.add('row-unassigned');

        // Checkbox cell
        const tdCheck = document.createElement('td');
        tdCheck.className = 'td-checkbox';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = state.selectedIds.has(rec.id);
        checkbox.addEventListener('change', (e) => {
          if (e.target.checked) {
            state.selectedIds.add(rec.id);
          } else {
            state.selectedIds.delete(rec.id);
          }
          this.updateSelectionStats();
        });
        tdCheck.appendChild(checkbox);
        tr.appendChild(tdCheck);

        // ID cell
        const tdId = document.createElement('td');
        tdId.className = 'td-id';
        const idLink = document.createElement('a');
        idLink.href = '#';
        idLink.className = 'ticket-id-link';
        idLink.textContent = rec.id;
        idLink.addEventListener('click', (e) => {
          e.preventDefault();
          this.openTicketModal(rec);
        });
        tdId.appendChild(idLink);
        tr.appendChild(tdId);

        // Priority cell
        const tdPriority = document.createElement('td');
        const prioBadge = document.createElement('span');
        const safePrioClass = String(rec.priority || 'medium').toLowerCase().replace(/[^a-z0-9_-]/g, '');
        prioBadge.className = `prio-pill prio-${safePrioClass}`;
        prioBadge.textContent = rec.priority;
        tdPriority.appendChild(prioBadge);
        tr.appendChild(tdPriority);

        // Status cell
        const tdStatus = document.createElement('td');
        const statusBadge = document.createElement('span');
        const statusClass = String(rec.status || 'open').toLowerCase().replace(/[^a-z0-9_-]/g, '-');
        statusBadge.className = `status-pill status-${statusClass}`;
        statusBadge.textContent = rec.status;
        tdStatus.appendChild(statusBadge);
        tr.appendChild(tdStatus);

        // Title & description snippet cell
        const tdTitle = document.createElement('td');
        tdTitle.className = 'td-title';
        const titleDiv = document.createElement('div');
        titleDiv.className = 'ticket-title-text';
        titleDiv.textContent = rec.title;
        titleDiv.title = rec.title;
        titleDiv.addEventListener('click', () => this.openTicketModal(rec));

        const descSnippet = document.createElement('div');
        descSnippet.className = 'ticket-desc-snippet';
        descSnippet.textContent = rec.description || 'No description provided';

        titleDiv.appendChild(descSnippet);
        tdTitle.appendChild(titleDiv);

        // Render tags if any
        if (rec.tags && rec.tags.length > 0) {
          const tagsDiv = document.createElement('div');
          tagsDiv.className = 'ticket-tag-list';
          rec.tags.slice(0, 3).forEach(t => {
            const tagSpan = document.createElement('span');
            tagSpan.className = 'tag-chip';
            tagSpan.textContent = t;
            tagsDiv.appendChild(tagSpan);
          });
          tdTitle.appendChild(tagsDiv);
        }

        tr.appendChild(tdTitle);

        // Assignee cell
        const tdAssignee = document.createElement('td');
        tdAssignee.className = 'td-assignee';
        if (rec.assignee) {
          tdAssignee.textContent = rec.assignee;
        } else {
          const unass = document.createElement('span');
          unass.className = 'unassigned-pill';
          unass.textContent = 'Unassigned';
          tdAssignee.appendChild(unass);
        }
        tr.appendChild(tdAssignee);

        // Customer cell
        const tdCustomer = document.createElement('td');
        tdCustomer.className = 'td-customer';
        tdCustomer.textContent = rec.customer;
        tdCustomer.title = rec.customer;
        tr.appendChild(tdCustomer);

        // Duplicate relation cell
        const tdDup = document.createElement('td');
        tdDup.className = 'td-dup';
        if (rec.is_duplicate) {
          const dupBtn = document.createElement('button');
          const safeRole = rec.duplicate_role === 'primary' ? 'primary' : 'duplicate';
          dupBtn.className = `dup-indicator-btn ${safeRole}`;
          dupBtn.textContent = rec.duplicate_role === 'primary' ? `★ Primary (${rec.duplicate_group_id})` : `⧉ Dup (${rec.duplicate_group_id})`;
          dupBtn.title = `Click to inspect duplicate group ${rec.duplicate_group_id}`;
          dupBtn.addEventListener('click', () => this.inspectDuplicateGroup(rec.duplicate_group_id));
          tdDup.appendChild(dupBtn);
        } else {
          const dash = document.createElement('span');
          dash.className = 'text-muted';
          dash.textContent = '–';
          tdDup.appendChild(dash);
        }
        tr.appendChild(tdDup);

        // Date cell
        const tdDate = document.createElement('td');
        tdDate.className = 'td-date';
        try {
          const d = new Date(rec.created_at);
          tdDate.textContent = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
        } catch {
          tdDate.textContent = rec.raw_created_at || rec.created_at;
        }
        tr.appendChild(tdDate);

        // Action cell
        const tdAction = document.createElement('td');
        tdAction.className = 'td-actions';
        const viewBtn = document.createElement('button');
        viewBtn.className = 'btn-icon';
        viewBtn.title = 'View details';
        viewBtn.textContent = '👁';
        viewBtn.addEventListener('click', () => this.openTicketModal(rec));
        tdAction.appendChild(viewBtn);
        tr.appendChild(tdAction);

        tbody.appendChild(tr);
      }

      // Update select-all checkbox state
      const allSelected = state.filteredRecords.length > 0 && state.filteredRecords.every(r => state.selectedIds.has(r.id));
      this.dom.selectAllCheckbox.checked = allSelected;
    }

    static renderKanban() {
      const container = this.dom.kanbanViewContainer;
      container.replaceChildren();

      const columns = [
        { id: 'Open', title: 'Open', color: 'blue' },
        { id: 'In Progress', title: 'In Progress', color: 'amber' },
        { id: 'Pending Customer', title: 'Pending Customer', color: 'purple' },
        { id: 'Resolved', title: 'Resolved / Closed', color: 'green', statuses: ['Resolved', 'Closed'] }
      ];

      for (const col of columns) {
        const colDiv = document.createElement('div');
        colDiv.className = 'kanban-col';

        const matchingRecords = state.filteredRecords.filter(r => {
          if (col.statuses) return col.statuses.includes(r.status);
          return r.status === col.title;
        });

        const colHeader = document.createElement('div');
        colHeader.className = 'kanban-col-header';
        const titleSpan = document.createElement('span');
        titleSpan.className = 'kanban-title';
        titleSpan.textContent = col.title;
        const badgeSpan = document.createElement('span');
        badgeSpan.className = 'kanban-badge';
        badgeSpan.textContent = String(matchingRecords.length);
        colHeader.appendChild(titleSpan);
        colHeader.appendChild(badgeSpan);
        colDiv.appendChild(colHeader);

        const cardList = document.createElement('div');
        cardList.className = 'kanban-card-list';

        if (matchingRecords.length === 0) {
          const emptyCard = document.createElement('div');
          emptyCard.className = 'kanban-empty';
          emptyCard.textContent = 'No tickets in this column';
          cardList.appendChild(emptyCard);
        } else {
          for (const rec of matchingRecords) {
            const card = document.createElement('div');
            card.className = `kanban-card ${rec.is_duplicate ? 'card-duplicate' : ''}`;
            card.addEventListener('click', () => this.openTicketModal(rec));

            const cardHeader = document.createElement('div');
            cardHeader.className = 'card-header';
            const idSpan = document.createElement('span');
            idSpan.className = 'card-id';
            idSpan.textContent = rec.id;
            cardHeader.appendChild(idSpan);

            const prioSpan = document.createElement('span');
            const safePrioClass = String(rec.priority || 'medium').toLowerCase().replace(/[^a-z0-9_-]/g, '');
            prioSpan.className = `prio-pill prio-${safePrioClass}`;
            prioSpan.textContent = rec.priority;
            cardHeader.appendChild(prioSpan);
            card.appendChild(cardHeader);

            const cardTitle = document.createElement('div');
            cardTitle.className = 'card-title';
            cardTitle.textContent = rec.title;
            card.appendChild(cardTitle);

            const cardMeta = document.createElement('div');
            cardMeta.className = 'card-meta';
            const assigneeSpan = document.createElement('span');
            assigneeSpan.className = 'card-assignee';
            assigneeSpan.textContent = rec.assignee ? rec.assignee : '⚠️ Unassigned';
            cardMeta.appendChild(assigneeSpan);

            if (rec.is_duplicate) {
              const dupBadge = document.createElement('span');
              dupBadge.className = 'card-dup-badge';
              dupBadge.textContent = rec.duplicate_group_id || 'Duplicate';
              cardMeta.appendChild(dupBadge);
            }
            card.appendChild(cardMeta);

            cardList.appendChild(card);
          }
        }

        colDiv.appendChild(cardList);
        container.appendChild(colDiv);
      }
    }

    static updateSelectionStats() {
      const count = state.selectedIds.size;
      this.dom.selectionCountText.textContent = `${count} selected`;
      this.dom.exportSelectedCsvBtn.textContent = `Export Selected as CSV (${count})`;
      this.dom.exportSelectedJsonBtn.textContent = `Export Selected as JSON (${count})`;

      const disabled = count === 0;
      this.dom.exportSelectedCsvBtn.classList.toggle('disabled', disabled);
      this.dom.exportSelectedJsonBtn.classList.toggle('disabled', disabled);
    }

    // --- Detail & Inspector Modals ---

    static openTicketModal(record) {
      const modal = this.dom.ticketModal;
      const content = this.dom.ticketModalContent;

      let duplicateHtml = '<p class="text-muted">None (Record is unique)</p>';
      if (record.is_duplicate && record.duplicate_group_id) {
        const group = state.duplicateGroups.get(record.duplicate_group_id) || [];
        const siblings = group.filter(r => r.id !== record.id);
        const safeRoleClass = escapeHtml(String(record.duplicate_role || '').toLowerCase().replace(/[^a-z0-9_-]/g, ''));
        const safeRoleText = escapeHtml(String(record.duplicate_role || '').toUpperCase());
        duplicateHtml = `
          <div class="duplicate-relation-box">
            <div class="dup-box-header">
              <strong>Duplicate Group: ${escapeHtml(record.duplicate_group_id)}</strong>
              <span class="dup-role-badge ${safeRoleClass}">${safeRoleText}</span>
            </div>
            <p class="dup-box-sub">Related records matching issue criteria:</p>
            <ul class="dup-sibling-list">
              ${siblings.map(s => `
                <li>
                  <a href="#" class="sibling-link" data-id="${escapeHtml(s.id)}"><strong>${escapeHtml(s.id)}</strong>: ${escapeHtml(s.title)}</a>
                  <span class="text-muted">(${escapeHtml(s.status)} | ${escapeHtml(s.customer)})</span>
                </li>
              `).join('')}
            </ul>
          </div>
        `;
      }

      // Custom fields rendering
      let customFieldsHtml = '<p class="text-muted">None</p>';
      if (record.custom_fields && Object.keys(record.custom_fields).length > 0) {
        customFieldsHtml = `
          <table class="custom-fields-table">
            <thead><tr><th>Field Name</th><th>Preserved Value</th></tr></thead>
            <tbody>
              ${Object.entries(record.custom_fields).map(([k, v]) => `
                <tr><td><code>${escapeHtml(k)}</code></td><td>${escapeHtml(String(v))}</td></tr>
              `).join('')}
            </tbody>
          </table>
        `;
      }

      const safeModalPrioClass = escapeHtml(String(record.priority || '').toLowerCase().replace(/[^a-z0-9_-]/g, ''));
      const safeModalStatusClass = escapeHtml(String(record.status || '').toLowerCase().replace(/[^a-z0-9_-]/g, '-'));

      content.innerHTML = `
        <div class="modal-ticket-header">
          <div class="modal-title-row">
            <span class="modal-ticket-id">${escapeHtml(record.id)}</span>
            <span class="prio-pill prio-${safeModalPrioClass}">${escapeHtml(record.priority)}</span>
            <span class="status-pill status-${safeModalStatusClass}">${escapeHtml(record.status)}</span>
          </div>
          <h2 class="modal-ticket-title">${escapeHtml(record.title)}</h2>
        </div>

        <div class="modal-grid">
          <div class="modal-main-col">
            <div class="detail-section">
              <h3>Description</h3>
              <div class="detail-body-text">${escapeHtml(record.description || 'No description provided.')}</div>
            </div>

            <div class="detail-section">
              <h3>Duplicate Relationships</h3>
              ${duplicateHtml}
            </div>

            <div class="detail-section">
              <h3>Custom & Non-Standard Fields</h3>
              ${customFieldsHtml}
            </div>
          </div>

          <div class="modal-side-col">
            <div class="side-info-card">
              <h4>Metadata</h4>
              <dl class="info-dl">
                <dt>Customer / Requester</dt>
                <dd>${escapeHtml(record.customer)}</dd>
                <dt>Assignee</dt>
                <dd>${record.assignee ? escapeHtml(record.assignee) : '<span class="unassigned-pill">Unassigned</span>'}</dd>
                <dt>Channel</dt>
                <dd>${escapeHtml(record.channel || 'Direct')}</dd>
                <dt>Created Timestamp</dt>
                <dd>${escapeHtml(record.created_at)}</dd>
                <dt>Original Source Row</dt>
                <dd>Line ${escapeHtml(String(record.row_number))}</dd>
                <dt>Tags</dt>
                <dd>${record.tags && record.tags.length > 0 ? record.tags.map(t => `<span class="tag-chip">${escapeHtml(t)}</span>`).join(' ') : 'None'}</dd>
              </dl>
            </div>

            <div class="raw-json-box">
              <h4>Raw Record JSON</h4>
              <pre><code>${escapeHtml(JSON.stringify(record, null, 2))}</code></pre>
            </div>
          </div>
        </div>
      `;

      // Attach click events to sibling links inside modal
      content.querySelectorAll('.sibling-link').forEach(link => {
        link.addEventListener('click', (e) => {
          e.preventDefault();
          const targetId = link.getAttribute('data-id');
          const targetRecord = state.records.find(r => r.id === targetId);
          if (targetRecord) {
            this.openTicketModal(targetRecord);
          }
        });
      });

      modal.classList.add('open');
    }

    static closeTicketModal() {
      this.dom.ticketModal.classList.remove('open');
    }

    static openDiagnosticsModal() {
      const modal = this.dom.diagnosticsModal;
      const body = this.dom.diagnosticsModalBody;
      const diag = state.diagnostics;

      body.innerHTML = `
        <div class="diag-overview-cards">
          <div class="diag-stat-card">
            <div class="diag-stat-num">${diag.totalParsed}</div>
            <div class="diag-stat-lbl">Total Input Lines</div>
          </div>
          <div class="diag-stat-card success">
            <div class="diag-stat-num">${diag.usableCount}</div>
            <div class="diag-stat-lbl">Usable Records Ingested</div>
          </div>
          <div class="diag-stat-card warning">
            <div class="diag-stat-num">${diag.warningCount}</div>
            <div class="diag-stat-lbl">Warnings Recovered</div>
          </div>
          <div class="diag-stat-card error">
            <div class="diag-stat-num">${diag.excludedCount}</div>
            <div class="diag-stat-lbl">Excluded / Malformed</div>
          </div>
        </div>

        <div class="diag-tabs">
          <button class="diag-tab-btn active" id="diag-tab-excluded">Excluded Records (${diag.excludedCount})</button>
          <button class="diag-tab-btn" id="diag-tab-warnings">Recovered with Warnings (${diag.warningCount})</button>
        </div>

        <div id="diag-tab-content-excluded" class="diag-tab-pane active">
          ${diag.excludedCount === 0 ? `
            <div class="clean-empty-box">
              <div class="clean-check-icon">✓</div>
              <p><strong>Zero records excluded!</strong> All input data rows were successfully identified and imported.</p>
            </div>
          ` : `
            <table class="diag-table">
              <thead>
                <tr>
                  <th style="width: 80px;">Row #</th>
                  <th style="width: 250px;">Exclusion Reason</th>
                  <th>Raw Content Preview</th>
                </tr>
              </thead>
              <tbody>
                ${diag.excluded.map(ex => `
                  <tr>
                    <td><code>Line ${escapeHtml(String(ex.row))}</code></td>
                    <td><span class="reason-pill">${escapeHtml(ex.reason)}</span></td>
                    <td><pre class="raw-data-snippet">${escapeHtml(ex.rawData || '(empty line)')}</pre></td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          `}
        </div>

        <div id="diag-tab-content-warnings" class="diag-tab-pane">
          ${diag.warningCount === 0 ? `
            <div class="clean-empty-box">
              <div class="clean-check-icon">✓</div>
              <p><strong>No missing fields or anomalies detected.</strong> All tickets contained clean standard values.</p>
            </div>
          ` : `
            <table class="diag-table">
              <thead>
                <tr>
                  <th style="width: 80px;">Row #</th>
                  <th style="width: 110px;">Ticket ID</th>
                  <th style="width: 100px;">Field</th>
                  <th>Explanation & Applied Safe Default</th>
                </tr>
              </thead>
              <tbody>
                ${diag.warnings.map(w => `
                  <tr>
                    <td><code>Line ${escapeHtml(String(w.row))}</code></td>
                    <td><strong>${escapeHtml(w.id)}</strong></td>
                    <td><span class="field-badge">${escapeHtml(w.field)}</span></td>
                    <td>${escapeHtml(w.message)}</td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          `}
        </div>
      `;

      // Tab switching
      const tabExcludedBtn = document.getElementById('diag-tab-excluded');
      const tabWarningsBtn = document.getElementById('diag-tab-warnings');
      const paneExcluded = document.getElementById('diag-tab-content-excluded');
      const paneWarnings = document.getElementById('diag-tab-content-warnings');

      tabExcludedBtn.addEventListener('click', () => {
        tabExcludedBtn.classList.add('active');
        tabWarningsBtn.classList.remove('active');
        paneExcluded.classList.add('active');
        paneWarnings.classList.remove('active');
      });

      tabWarningsBtn.addEventListener('click', () => {
        tabWarningsBtn.classList.add('active');
        tabExcludedBtn.classList.remove('active');
        paneWarnings.classList.add('active');
        paneExcluded.classList.remove('active');
      });

      modal.classList.add('open');
    }

    static closeDiagnosticsModal() {
      this.dom.diagnosticsModal.classList.remove('open');
    }

    static exportDiagnostics() {
      const diagData = {
        generated_at: new Date().toISOString(),
        summary: {
          total_parsed_lines: state.diagnostics.totalParsed,
          usable_records_imported: state.diagnostics.usableCount,
          warnings_recovered: state.diagnostics.warningCount,
          excluded_records: state.diagnostics.excludedCount
        },
        excluded_records: state.diagnostics.excluded,
        recovered_warnings: state.diagnostics.warnings
      };
      ExportEngine.downloadFile('queuelens_ingestion_diagnostics.json', JSON.stringify(diagData, null, 2), 'application/json');
      this.showToast('Downloaded ingestion diagnostics log', 'success');
    }

    static openDuplicatesModal() {
      const modal = this.dom.duplicateModal;
      const body = this.dom.duplicateModalBody;

      if (state.duplicateGroups.size === 0) {
        body.innerHTML = `
          <div class="clean-empty-box">
            <div class="clean-check-icon">✓</div>
            <p><strong>No duplicate tickets detected in current backlog.</strong></p>
          </div>
        `;
      } else {
        let html = `
          <p class="modal-sub-text">Found <strong>${state.duplicateGroups.size} duplicate clusters</strong> based on normalized subject and requester similarity matching:</p>
          <div class="dup-groups-container">
        `;

        for (const [groupId, records] of state.duplicateGroups) {
          const primary = records.find(r => r.duplicate_role === 'primary') || records[0];
          const dups = records.filter(r => r.id !== primary.id);

          const safePrimaryPrioClass = escapeHtml(String(primary.priority || '').toLowerCase().replace(/[^a-z0-9_-]/g, ''));
          html += `
            <div class="dup-group-card">
              <div class="dup-group-header">
                <span class="dup-group-title">Duplicate Cluster ${escapeHtml(groupId)} (${records.length} tickets)</span>
                <button class="btn btn-sm btn-outline filter-group-btn" data-group="${escapeHtml(groupId)}">Filter to this group</button>
              </div>
              <div class="dup-cards-compare">
                <div class="dup-col primary">
                  <span class="dup-col-label primary">★ Primary (Earliest)</span>
                  <div class="dup-ticket-box">
                    <div class="dup-t-id">${escapeHtml(primary.id)} <span class="prio-pill prio-${safePrimaryPrioClass}">${escapeHtml(primary.priority)}</span></div>
                    <div class="dup-t-title">${escapeHtml(primary.title)}</div>
                    <div class="dup-t-meta">${escapeHtml(primary.customer)} • ${escapeHtml(primary.status)} • ${escapeHtml(primary.assignee || 'Unassigned')}</div>
                    <div class="dup-t-desc">${escapeHtml(primary.description || '')}</div>
                  </div>
                </div>

                <div class="dup-col duplicates">
                  <span class="dup-col-label duplicates">⧉ Identified Duplicates</span>
                  ${dups.map(dup => {
                    const safeDupPrioClass = escapeHtml(String(dup.priority || '').toLowerCase().replace(/[^a-z0-9_-]/g, ''));
                    return `
                    <div class="dup-ticket-box">
                      <div class="dup-t-id">${escapeHtml(dup.id)} <span class="prio-pill prio-${safeDupPrioClass}">${escapeHtml(dup.priority)}</span></div>
                      <div class="dup-t-title">${escapeHtml(dup.title)}</div>
                      <div class="dup-t-meta">${escapeHtml(dup.customer)} • ${escapeHtml(dup.status)} • ${escapeHtml(dup.assignee || 'Unassigned')}</div>
                      <div class="dup-t-desc">${escapeHtml(dup.description || '')}</div>
                    </div>
                    `;
                  }).join('')}
                </div>
              </div>
            </div>
          `;
        }

        html += '</div>';
        body.innerHTML = html;

        // Attach filter group actions
        body.querySelectorAll('.filter-group-btn').forEach(btn => {
          btn.addEventListener('click', () => {
            const grp = btn.getAttribute('data-group');
            this.inspectDuplicateGroup(grp);
            this.closeDuplicatesModal();
          });
        });
      }

      modal.classList.add('open');
    }

    static closeDuplicatesModal() {
      this.dom.duplicateModal.classList.remove('open');
    }

    static inspectDuplicateGroup(groupId) {
      // Filter the main view to only records in this duplicate group
      state.filters.search = '';
      state.filters.priority = 'ALL';
      state.filters.status = 'ALL';
      state.filters.assignee = 'ALL';
      state.filters.duplicate = 'DUPLICATES_ONLY';

      this.dom.searchInput.value = '';
      this.dom.priorityFilter.value = 'ALL';
      this.dom.statusFilter.value = 'ALL';
      this.dom.assigneeFilter.value = 'ALL';
      this.dom.duplicateFilter.value = 'DUPLICATES_ONLY';

      // Custom filter to this specific group
      state.filteredRecords = state.records.filter(r => r.duplicate_group_id === groupId);
      this.renderStats();
      this.renderActiveChips();
      this.renderTableRows();

      this.showToast(`Filtered to Duplicate Cluster ${groupId}`, 'info');
    }

    static openImportModal() {
      this.dom.importModal.classList.add('open');
    }

    static closeImportModal() {
      this.dom.importModal.classList.remove('open');
    }

    static closeAllModals() {
      this.closeTicketModal();
      this.closeDiagnosticsModal();
      this.closeDuplicatesModal();
      this.closeImportModal();
    }

    // --- Exports ---

    static exportFiltered(format) {
      if (state.filteredRecords.length === 0) {
        this.showToast('No records match current filter to export', 'warning');
        return;
      }
      const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
      if (format === 'csv') {
        const csv = ExportEngine.toCSV(state.filteredRecords);
        ExportEngine.downloadFile(`queuelens_filtered_${timestamp}.csv`, csv, 'text/csv');
      } else {
        const json = ExportEngine.toJSON(state.filteredRecords);
        ExportEngine.downloadFile(`queuelens_filtered_${timestamp}.json`, json, 'application/json');
      }
      this.showToast(`Exported ${state.filteredRecords.length} records as ${format.toUpperCase()}`, 'success');
    }

    static exportSelected(format) {
      if (state.selectedIds.size === 0) {
        this.showToast('No records currently selected', 'warning');
        return;
      }
      const selectedRecords = state.records.filter(r => state.selectedIds.has(r.id));
      const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);

      if (format === 'csv') {
        const csv = ExportEngine.toCSV(selectedRecords);
        ExportEngine.downloadFile(`queuelens_selected_${timestamp}.csv`, csv, 'text/csv');
      } else {
        const json = ExportEngine.toJSON(selectedRecords);
        ExportEngine.downloadFile(`queuelens_selected_${timestamp}.json`, json, 'application/json');
      }
      this.showToast(`Exported ${selectedRecords.length} selected records as ${format.toUpperCase()}`, 'success');
    }

    // --- Toast Notifications ---

    static showToast(message, type = 'info') {
      const container = this.dom.toastContainer;
      const toast = document.createElement('div');
      toast.className = `toast toast-${type}`;
      const iconSpan = document.createElement('span');
      iconSpan.className = 'toast-icon';
      iconSpan.textContent = type === 'success' ? '✓' : type === 'warning' ? '⚠️' : type === 'error' ? '✕' : 'ℹ';
      const msgSpan = document.createElement('span');
      msgSpan.className = 'toast-msg';
      msgSpan.textContent = message;
      toast.appendChild(iconSpan);
      toast.appendChild(msgSpan);
      container.appendChild(toast);

      setTimeout(() => {
        toast.classList.add('show');
      }, 10);

      setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
      }, 3500);
    }
  }

  // --- Utility helper ---
  function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  // Initialize upon DOM readiness
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => UIController.init());
  } else {
    UIController.init();
  }

  // Expose core classes on window for automated verification/testing
  window.QueueLens = {
    state,
    CSVParser,
    RecordNormalizer,
    DuplicateDetector,
    FilterEngine,
    ExportEngine,
    UIController,
    escapeHtml
  };

})();
