#!/usr/bin/env python3
"""
QueueLens Automated Verification Test Suite
Tests:
1. Static asset integrity and local-only (zero external network calls) guarantee.
2. CSV & JSON sample data validity and edge cases.
3. Node.js execution of app.js core engines:
   - RFC 4180 CSV parser & auto-delimiter detection
   - JSON structure normalization
   - Malformed/empty record detection & explanation
   - Safe defaults & data preservation (no silent loss of usable records)
   - Duplicate detection engine (exact & fuzzy clustering, primary identification)
   - Filter and search engine (priority, status, assignee, text)
   - Export engine (RFC 4180 CSV and JSON generation)
"""

import os
import sys
import json
import csv
import re
import subprocess

APP_DIR = os.path.dirname(os.path.abspath(__file__))

def assert_true(condition, message):
    if not condition:
        print(f"  [FAIL] {message}")
        sys.exit(1)
    print(f"  [PASS] {message}")

def test_file_structure():
    print("\n--- Test 1: File Structure & Required Assets ---")
    required_files = [
        "index.html",
        "styles.css",
        "app.js",
        "sample-data.js",
        "server.py",
        "run.sh",
        "sample-data/support_tickets_standard.csv",
        "sample-data/support_tickets_standard.json",
        "sample-data/support_tickets_with_issues.csv",
        "sample-data/support_tickets_with_issues.json"
    ]
    for rel_path in required_files:
        full_path = os.path.join(APP_DIR, rel_path)
        assert_true(os.path.exists(full_path), f"Required file exists: {rel_path}")

def test_local_only_isolation():
    print("\n--- Test 2: Local-Only Isolation (Zero External Network Requests) ---")
    files_to_check = ["index.html", "styles.css", "app.js", "sample-data.js"]
    # Disallowed external protocols or hosts
    forbidden_patterns = [
        re.compile(r'https?://(?!127\.0\.0\.1|localhost)', re.IGNORECASE),
        re.compile(r'//fonts\.(googleapis|gstatic)\.com', re.IGNORECASE),
        re.compile(r'//cdnjs\.cloudflare\.com', re.IGNORECASE),
        re.compile(r'//cdn\.jsdelivr\.net', re.IGNORECASE),
        re.compile(r'//unpkg\.com', re.IGNORECASE),
        re.compile(r'<script\s+[^>]*src=[\'"]https?://', re.IGNORECASE),
        re.compile(r'<link\s+[^>]*href=[\'"]https?://', re.IGNORECASE)
    ]

    for filename in files_to_check:
        path = os.path.join(APP_DIR, filename)
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        for pattern in forbidden_patterns:
            matches = pattern.findall(content)
            assert_true(len(matches) == 0, f"No forbidden external URL references in {filename} (matches: {matches})")

def test_sample_data_files():
    print("\n--- Test 3: Sample Data Files Integrity ---")
    # Standard CSV
    csv_std_path = os.path.join(APP_DIR, "sample-data/support_tickets_standard.csv")
    with open(csv_std_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        std_rows = list(reader)
    assert_true(len(std_rows) == 20, f"Standard CSV contains exactly 20 records (found {len(std_rows)})")
    assert_true("id" in std_rows[0] and "title" in std_rows[0] and "priority" in std_rows[0], "Standard CSV has id, title, priority")

    # Standard JSON
    json_std_path = os.path.join(APP_DIR, "sample-data/support_tickets_standard.json")
    with open(json_std_path, "r", encoding="utf-8") as f:
        json_std = json.load(f)
    assert_true(isinstance(json_std, list) and len(json_std) == 20, f"Standard JSON contains 20 items (found {len(json_std)})")

    # Issues CSV
    csv_issues_path = os.path.join(APP_DIR, "sample-data/support_tickets_with_issues.csv")
    with open(csv_issues_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    assert_true(len(lines) > 5, "Issues CSV contains raw test rows including empty and malformed lines")

    # Issues JSON
    json_issues_path = os.path.join(APP_DIR, "sample-data/support_tickets_with_issues.json")
    with open(json_issues_path, "r", encoding="utf-8") as f:
        json_issues = json.load(f)
    assert_true(isinstance(json_issues, list), "Issues JSON is a valid JSON list containing diverse records")

def test_engine_via_node():
    print("\n--- Test 4: JavaScript Core Engine Unit Verification (via Node.js) ---")
    test_script = """
    const fs = require('fs');
    const path = require('path');

    // Setup mock DOM / browser globals
    global.window = { QUEUELENS_SAMPLES: {} };
    global.document = {
      readyState: 'complete',
      addEventListener: () => {},
      getElementById: () => null,
      documentElement: { setAttribute: () => {}, removeAttribute: () => {} }
    };
    global.localStorage = { getItem: () => null, setItem: () => {} };

    // Load sample-data.js and app.js
    eval(fs.readFileSync(path.join(__dirname, 'sample-data.js'), 'utf8'));
    eval(fs.readFileSync(path.join(__dirname, 'app.js'), 'utf8'));

    const { CSVParser, RecordNormalizer, DuplicateDetector, FilterEngine, ExportEngine } = window.QueueLens;

    // Strict assertion harness (R03-F03): throw and exit with non-zero code on any failure
    function assert(condition, message) {
      if (!condition) {
        console.error('ASSERTION_FAILED: ' + (message || 'unspecified failure'));
        process.exit(1);
      }
    }
    console.assert = assert;

    // 1. Test CSV Parser with quotes, commas, and newlines
    const testCsv = 'id,title,description\\nT-1,"Payment failed, urgent!","Line 1\\nLine 2"\\nT-2,"Normal title","Just notes"';
    const parsedCsv = CSVParser.parse(testCsv);
    assert(parsedCsv.rawRows.length === 3, 'CSV Parser: expected 3 rows including header');
    assert(parsedCsv.rawRows[1].fields[1] === 'Payment failed, urgent!', 'CSV Parser: handles commas inside quotes');
    assert(parsedCsv.rawRows[1].fields[2].includes('\\n'), 'CSV Parser: handles multiline fields');

    // 2. Test Record Normalization on Standard Sample
    const stdSample = window.QUEUELENS_SAMPLES.standard.data;
    const jsonResult = RecordNormalizer.normalizeJSON(JSON.stringify(stdSample));
    assert(jsonResult.records.length === 20, 'JSON Normalizer: parsed all 20 clean records');
    assert(jsonResult.excluded.length === 0, 'JSON Normalizer: 0 excluded records from clean sample');

    // 3. Test Ingestion with Malformed and Excluded records
    const issuesCsv = window.QUEUELENS_SAMPLES.issues.rawCsv;
    const issuesParsed = CSVParser.parse(issuesCsv);
    const issuesNorm = RecordNormalizer.normalizeCSV(issuesParsed.rawRows, issuesParsed.errors);
    assert(issuesNorm.excluded.length > 0, 'Normalization: correctly identifies and excludes unusable rows (blank, missing required fields)');
    assert(issuesNorm.warnings.length > 0, 'Normalization: correctly flags warnings for recovered fields (missing priority, extra columns)');
    assert(issuesNorm.records.length > 0, 'Normalization: does NOT lose usable records despite malformed rows');

    // Check specific recovery: missing ID gets synthetic ID
    const recoveredMissingId = issuesNorm.records.find(r => r.title.includes('Missing ID completely'));
    assert(recoveredMissingId && recoveredMissingId.id.startsWith('AUTO-'), 'Normalization: auto-assigns synthetic ID for ticket with title/desc but missing ID');

    // 4. Test Duplicate Detection Engine
    const duplicateGroups = DuplicateDetector.analyze(jsonResult.records);
    assert(duplicateGroups.size > 0, 'DuplicateDetector: successfully detected duplicate groups in standard backlog');

    // TICK-1001 and TICK-1003 are duplicates in standard sample
    const t1001 = jsonResult.records.find(r => r.id === 'TICK-1001');
    const t1003 = jsonResult.records.find(r => r.id === 'TICK-1003');
    assert(t1001 && t1001.is_duplicate, 'DuplicateDetector: TICK-1001 marked as duplicate');
    assert(t1003 && t1003.is_duplicate, 'DuplicateDetector: TICK-1003 marked as duplicate');
    assert(t1001.duplicate_group_id === t1003.duplicate_group_id, 'DuplicateDetector: TICK-1001 and TICK-1003 in same cluster');
    assert(t1001.duplicate_role === 'primary', 'DuplicateDetector: TICK-1001 is primary');
    assert(t1003.duplicate_role === 'duplicate', 'DuplicateDetector: TICK-1003 is duplicate');

    // 5. Test Filter Engine
    // Filter Priority
    const urgentTickets = FilterEngine.apply(jsonResult.records, { priority: 'Urgent', status: 'ALL', assignee: 'ALL', duplicate: 'ALL', search: '' }, { field: 'created_at', direction: 'desc' });
    assert(urgentTickets.every(r => r.priority === 'Urgent'), 'FilterEngine: all returned records have Urgent priority');

    // Filter Status
    const openTickets = FilterEngine.apply(jsonResult.records, { priority: 'ALL', status: 'Open', assignee: 'ALL', duplicate: 'ALL', search: '' }, { field: 'created_at', direction: 'desc' });
    assert(openTickets.every(r => r.status === 'Open'), 'FilterEngine: all returned records have Open status');

    // Filter Assignee Unassigned
    const unassigned = FilterEngine.apply(jsonResult.records, { priority: 'ALL', status: 'ALL', assignee: '__UNASSIGNED__', duplicate: 'ALL', search: '' }, { field: 'created_at', direction: 'desc' });
    assert(unassigned.every(r => !r.assignee), 'FilterEngine: unassigned filter works correctly');

    // Full text search
    const searchResults = FilterEngine.apply(jsonResult.records, { priority: 'ALL', status: 'ALL', assignee: 'ALL', duplicate: 'ALL', search: 'payment' }, { field: 'created_at', direction: 'desc' });
    assert(searchResults.length > 0 && searchResults.every(r => r.title.toLowerCase().includes('payment') || r.description.toLowerCase().includes('payment')), 'FilterEngine: full text search matches relevant tickets');

    // 6. Test Export Engine & SEC-002 Formula Injection Protection
    const exportedCsv = ExportEngine.toCSV(jsonResult.records);
    assert(exportedCsv.startsWith('id,title,description'), 'ExportEngine: CSV has valid header row');
    assert(exportedCsv.split('\\r\\n').length === 21, 'ExportEngine: CSV includes header + 20 ticket lines');

    const exportedJson = ExportEngine.toJSON(jsonResult.records);
    const reParsed = JSON.parse(exportedJson);
    assert(reParsed.length === 20, 'ExportEngine: JSON matches original record count');

    // SEC-001: Unit test escapeHtml utility with XSS vectors
    const { escapeHtml } = window.QueueLens;
    assert(escapeHtml('<script>alert("xss")</script>') === '&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;', 'escapeHtml: tags and double quotes escaped');
    assert(escapeHtml('"><img src=x onerror=alert(1)>') === '&quot;&gt;&lt;img src=x onerror=alert(1)&gt;', 'escapeHtml: attribute breakouts and img onerror escaped');
    assert(escapeHtml("It's a 'test' & 1 < 2") === 'It&#039;s a &#039;test&#039; &amp; 1 &lt; 2', 'escapeHtml: single quotes and ampersand escaped');
    assert(escapeHtml(null) === '', 'escapeHtml: null returns empty string');
    assert(escapeHtml(undefined) === '', 'escapeHtml: undefined returns empty string');

    // SEC-002: Test CSV Formula Injection Neutralization
    const formulaPayloadRecords = [
      { id: "T-F1", title: "=SUM(1,2)", description: "+cmd|/C calc!A0", priority: "High", status: "Open", assignee: "@admin", customer: "-User1", created_at: "2026-01-01T00:00:00Z", tags: [] },
      { id: "T-F2", title: "  =1+1", description: "\\tmalicious_tab", priority: "Low", status: "Closed", assignee: "", customer: "NormalCorp", created_at: "2026-01-01T00:00:00Z", tags: [] },
      { id: "T-F3", title: "Normal Ticket Title", description: "Standard issue description", priority: "Medium", status: "Open", assignee: "Jane", customer: "Acme", created_at: "2026-01-01T00:00:00Z", tags: [] }
    ];

    const formulaCsv = ExportEngine.toCSV(formulaPayloadRecords);
    // Verify each formula prefix is prepended with a single quote in CSV
    assert(formulaCsv.indexOf("'" + "=SUM(1,2)") !== -1, 'ExportEngine (SEC-002): = prefix neutralized with single quote');
    assert(formulaCsv.indexOf("'" + "  =1+1") !== -1, 'ExportEngine (SEC-002): whitespace-prefixed = neutralized');
    assert(formulaCsv.indexOf("'" + "\tmalicious_tab") !== -1, 'ExportEngine (SEC-002): tab prefix neutralized');
    assert(formulaCsv.indexOf("'" + "@admin") !== -1, 'ExportEngine (SEC-002): @ prefix neutralized');
    assert(formulaCsv.indexOf("'" + "-User1") !== -1, 'ExportEngine (SEC-002): customer - prefix neutralized');
    assert(formulaCsv.indexOf("'" + "+cmd|/C calc!A0") !== -1, 'ExportEngine (SEC-002): + prefix neutralized');
    assert(formulaCsv.indexOf('"Normal Ticket Title"') !== -1, 'ExportEngine (SEC-002): benign text title is not prefixed');

    // Verify JSON export preserves original values completely unmodified
    const formulaJson = ExportEngine.toJSON(formulaPayloadRecords);
    const parsedFormulaJson = JSON.parse(formulaJson);
    assert(parsedFormulaJson[0].title === '=SUM(1,2)', 'ExportEngine (SEC-002): JSON export retains original =SUM without modification');
    assert(parsedFormulaJson[0].description === '+cmd|/C calc!A0', 'ExportEngine (SEC-002): JSON export retains original +cmd without modification');
    assert(parsedFormulaJson[0].assignee === '@admin', 'ExportEngine (SEC-002): JSON export retains original @admin without modification');

    // 7. Test R03-F02: Raw value preservation in custom_fields for unrecognized/null standard fields
    const testUnrec = [{ id: "X-1", priority: null, status: "weird-state" }];
    const unrecResult = RecordNormalizer.normalizeJSON(JSON.stringify(testUnrec));
    assert(unrecResult.records.length === 1, 'R03-F02: Normalizer keeps record with unrecognized/null fields');
    assert(unrecResult.records[0].priority === 'Medium', 'R03-F02: Unrecognized/null priority defaulted to Medium');
    assert(unrecResult.records[0].status === 'Open', 'R03-F02: Unrecognized status defaulted to Open');
    assert(unrecResult.records[0].custom_fields !== undefined, 'R03-F02: custom_fields exists on record');
    assert(unrecResult.records[0].custom_fields.priority === null || unrecResult.records[0].custom_fields.raw_priority === null, 'R03-F02: Raw null priority preserved in custom_fields');
    assert(unrecResult.records[0].custom_fields.status === 'weird-state' || unrecResult.records[0].custom_fields.raw_status === 'weird-state', 'R03-F02: Raw status preserved in custom_fields');
    const preservedWarnings = unrecResult.warnings.filter(w => w.message.includes('preserved in custom_fields'));
    assert(preservedWarnings.length === 2, 'R03-F02: Two warnings emitted for priority and status normalization');
    assert(preservedWarnings.every(w => w.message.includes('preserved in custom_fields')), 'R03-F02: Warning messages accurately explain preservation in custom_fields');

    // 8. Test CSVParser empty input return shape
    const emptyCsvParsed = CSVParser.parse('');
    assert(Array.isArray(emptyCsvParsed.rawRows) && emptyCsvParsed.rawRows.length === 0, 'CSVParser: parse("") returns rawRows array');
    assert(emptyCsvParsed.errors.length > 0, 'CSVParser: parse("") records diagnostic error');

    console.log('ALL_NODE_TESTS_PASSED');
    """;

    proc = subprocess.run(
        ["node", "-e", test_script],
        cwd=APP_DIR,
        capture_output=True,
        text=True
    )
    if proc.returncode != 0:
        print("Node test execution failed:")
        print(proc.stderr)
        sys.exit(1)
    assert_true("ALL_NODE_TESTS_PASSED" in proc.stdout, "Node.js engine test suite passed cleanly with all assertions verified")

    # Verify R03-F03: prove that a failing assertion in Node causes non-zero exit and test failure
    failing_test_script = """
    function assert(condition, message) {
      if (!condition) {
        console.error('ASSERTION_FAILED: ' + message);
        process.exit(1);
      }
    }
    console.assert = assert;
    console.assert(false, "Deliberate test failure for R03-F03 verification");
    """
    proc_fail = subprocess.run(
        ["node", "-e", failing_test_script],
        cwd=APP_DIR,
        capture_output=True,
        text=True
    )
    assert_true(proc_fail.returncode != 0, "R03-F03: Node assertion failure correctly exits with non-zero status code")
    assert_true("ASSERTION_FAILED: Deliberate test failure" in proc_fail.stderr, "R03-F03: Assertion failure message logged to stderr")

def test_security_server_loopback_restriction():
    print("\n--- Test 5: Security SEC-003 - Loopback-Only Server Binding ---")
    server_path = os.path.join(APP_DIR, "server.py")

    # 1. Test binding to 0.0.0.0 must be rejected
    proc_all = subprocess.run(
        [sys.executable, server_path, "--host", "0.0.0.0"],
        capture_output=True,
        text=True
    )
    assert_true(proc_all.returncode != 0, "server.py --host 0.0.0.0 is rejected with non-zero exit code")
    assert_true("prohibits binding to non-loopback interface" in proc_all.stderr, "Error message explicitly cites non-loopback prohibition")

    # 2. Test binding to arbitrary LAN IP must be rejected
    proc_lan = subprocess.run(
        [sys.executable, server_path, "--host", "192.168.1.100"],
        capture_output=True,
        text=True
    )
    assert_true(proc_lan.returncode != 0, "server.py --host 192.168.1.100 is rejected with non-zero exit code")

    # 3. Test binding to public IP must be rejected
    proc_pub = subprocess.run(
        [sys.executable, server_path, "--host", "8.8.8.8"],
        capture_output=True,
        text=True
    )
    assert_true(proc_pub.returncode != 0, "server.py --host 8.8.8.8 is rejected with non-zero exit code")

    # 4. Test loopback host verification in Python directly
    sys.path.insert(0, APP_DIR)
    import server
    assert_true(server.is_loopback("127.0.0.1"), "server.is_loopback('127.0.0.1') is True")
    assert_true(server.is_loopback("localhost"), "server.is_loopback('localhost') is True")
    assert_true(server.is_loopback("::1"), "server.is_loopback('::1') is True")
    assert_true(not server.is_loopback("0.0.0.0"), "server.is_loopback('0.0.0.0') is False")
    assert_true(not server.is_loopback("192.168.0.1"), "server.is_loopback('192.168.0.1') is False")

def test_security_csp_and_markup_hardening():
    print("\n--- Test 6: Security SEC-001 - CSP & Inline Handler Hardening ---")
    # 1. Verify CSP in server.py does not include 'unsafe-inline' in script-src
    server_path = os.path.join(APP_DIR, "server.py")
    with open(server_path, "r", encoding="utf-8") as f:
        server_code = f.read()
    csp_match = re.search(r'Content-Security-Policy["\'],\s*["\'](.*?)["\']\s*\)', server_code, re.DOTALL)
    assert_true(csp_match is not None, "Content-Security-Policy header defined in server.py")
    csp_value = csp_match.group(1)
    assert_true("script-src 'self'" in csp_value, f"CSP enforces script-src 'self' (got: {csp_value})")
    assert_true("script-src 'self' 'unsafe-inline'" not in csp_value, "CSP prohibits 'unsafe-inline' in script-src")

    # 2. Verify CSP meta tag in index.html
    index_path = os.path.join(APP_DIR, "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        index_html = f.read()
    assert_true('<meta http-equiv="Content-Security-Policy"' in index_html, "index.html contains CSP meta tag")
    assert_true("script-src 'self'" in index_html, "index.html CSP enforces script-src 'self'")

    # 3. Verify zero inline onclick or other event handlers in index.html
    inline_handlers = re.findall(r'\son[a-z]+=["\']', index_html, re.IGNORECASE)
    assert_true(len(inline_handlers) == 0, f"index.html has zero inline event handler attributes (found {len(inline_handlers)})")

if __name__ == "__main__":
    test_file_structure()
    test_local_only_isolation()
    test_sample_data_files()
    test_engine_via_node()
    test_security_server_loopback_restriction()
    test_security_csp_and_markup_hardening()
    print("\n==========================================")
    print(" ALL QUEUELENS AUTOMATED TESTS PASSED! ")
    print("==========================================\n")
