// QueueLens - Built-in Sample Datasets
// Self-contained, zero-network preloaded datasets for quick exploration and testing.

window.QUEUELENS_SAMPLES = {
  standard: {
    name: "Standard Support Backlog (20 tickets)",
    description: "Realistic multi-priority support backlog with IT/SaaS requests, assignees, tags, and known duplicate reports.",
    format: "json",
    data: [
      {
        "id": "TICK-1001",
        "title": "Unable to process credit card payment at checkout",
        "description": "Users report getting error code ERR_PAY_502 when submitting payment details using Visa cards. Multiple customers affected.",
        "priority": "Urgent",
        "status": "Open",
        "assignee": "Sarah Chen",
        "customer": "Acme Corp (billing@acme.com)",
        "created_at": "2026-08-28T08:15:00Z",
        "tags": ["billing", "payment", "urgent"],
        "channel": "Portal"
      },
      {
        "id": "TICK-1002",
        "title": "SSO login redirect loop on Google Workspace",
        "description": "After entering Google credentials, user is redirected back to the login prompt with no error message displayed.",
        "priority": "High",
        "status": "In Progress",
        "assignee": "Alex Rivera",
        "customer": "Globex Industries (it-support@globex.com)",
        "created_at": "2026-08-28T09:30:00Z",
        "tags": ["auth", "sso", "google"],
        "channel": "Portal"
      },
      {
        "id": "TICK-1003",
        "title": "Unable to process credit card payment at checkout",
        "description": "Customers experiencing ERR_PAY_502 on Visa payment gateway. Urgent fix needed.",
        "priority": "Urgent",
        "status": "Open",
        "assignee": "Sarah Chen",
        "customer": "Acme Corp (support@acme.com)",
        "created_at": "2026-08-28T09:45:00Z",
        "tags": ["billing", "payment"],
        "channel": "Email"
      },
      {
        "id": "TICK-1004",
        "title": "Request for audit logs export in CSV format",
        "description": "Need monthly compliance audit logs exported for Q2 2026. Current UI only allows JSON export.",
        "priority": "Low",
        "status": "Resolved",
        "assignee": "David Kim",
        "customer": "Initech LLC (compliance@initech.com)",
        "created_at": "2026-08-27T14:10:00Z",
        "tags": ["audit", "compliance", "export"],
        "channel": "Email"
      },
      {
        "id": "TICK-1005",
        "title": "Dashboard metrics showing 0 for active sessions",
        "description": "Real-time analytics widget has displayed zero active sessions since the 03:00 UTC deployment.",
        "priority": "High",
        "status": "In Progress",
        "assignee": "Elena Rostova",
        "customer": "Hooli (admin@hooli.com)",
        "created_at": "2026-08-28T07:22:00Z",
        "tags": ["analytics", "dashboard", "bug"],
        "channel": "Chat"
      },
      {
        "id": "TICK-1006",
        "title": "Password reset email not arriving in inbox",
        "description": "Requested password reset three times, checked spam and quarantine folders. No email received.",
        "priority": "Medium",
        "status": "Open",
        "assignee": "",
        "customer": "Stark Dynamics (tony@stark.com)",
        "created_at": "2026-08-28T10:05:00Z",
        "tags": ["auth", "email"],
        "channel": "Portal"
      },
      {
        "id": "TICK-1007",
        "title": "API rate limit exceeded unexpectedly on tier 3 plan",
        "description": "Our enterprise tier should permit 5000 req/min but we are getting 429 errors at approximately 1200 req/min.",
        "priority": "High",
        "status": "Open",
        "assignee": "Alex Rivera",
        "customer": "Umbrella Pharma (devops@umbrella.org)",
        "created_at": "2026-08-28T10:30:00Z",
        "tags": ["api", "rate-limit", "enterprise"],
        "channel": "API"
      },
      {
        "id": "TICK-1008",
        "title": "Webhook delivery failures returning HTTP 504",
        "description": "Downstream webhook endpoints are timing out during bulk inventory sync events.",
        "priority": "Medium",
        "status": "Pending Customer",
        "assignee": "David Kim",
        "customer": "Cyberdyne Systems (sysadmin@cyberdyne.io)",
        "created_at": "2026-08-27T16:45:00Z",
        "tags": ["webhook", "integration"],
        "channel": "Email"
      },
      {
        "id": "TICK-1009",
        "title": "Password reset email not arriving in inbox",
        "description": "Reset token emails are not being received. Checked spam folder.",
        "priority": "Medium",
        "status": "Open",
        "assignee": "",
        "customer": "Stark Dynamics (secops@stark.com)",
        "created_at": "2026-08-28T10:40:00Z",
        "tags": ["auth", "email"],
        "channel": "Email"
      },
      {
        "id": "TICK-1010",
        "title": "Typo on invoice PDF footer text",
        "description": "Company registration number has an extra digit printed in the footer of generated invoices.",
        "priority": "Low",
        "status": "Resolved",
        "assignee": "Sarah Chen",
        "customer": "Wayne Enterprises (accounts@wayne.org)",
        "created_at": "2026-08-26T11:00:00Z",
        "tags": ["invoice", "pdf", "cosmetic"],
        "channel": "Portal"
      },
      {
        "id": "TICK-1011",
        "title": "Slow page load times on reports view (over 12 seconds)",
        "description": "Generating the monthly team velocity report takes 12-15 seconds and frequently triggers gateway timeout.",
        "priority": "Medium",
        "status": "In Progress",
        "assignee": "Elena Rostova",
        "customer": "Pied Piper (richard@piedpiper.com)",
        "created_at": "2026-08-28T06:50:00Z",
        "tags": ["performance", "reports"],
        "channel": "Portal"
      },
      {
        "id": "TICK-1012",
        "title": "Dark mode toggle resets upon page refresh",
        "description": "User preference for dark theme is not persisting across session reloads or browser restarts.",
        "priority": "Low",
        "status": "Open",
        "assignee": "",
        "customer": "Massive Dynamic (ux@massivedynamic.com)",
        "created_at": "2026-08-28T11:15:00Z",
        "tags": ["ui", "theme", "preferences"],
        "channel": "Portal"
      },
      {
        "id": "TICK-1013",
        "title": "PDF export clips rightmost table column",
        "description": "When exporting tabular reports containing more than 8 columns, the last column is truncated on A4 paper.",
        "priority": "Low",
        "status": "Pending Customer",
        "assignee": "David Kim",
        "customer": "Oscorp (lab-ops@oscorp.com)",
        "created_at": "2026-08-27T13:20:00Z",
        "tags": ["export", "pdf", "table"],
        "channel": "Email"
      },
      {
        "id": "TICK-1014",
        "title": "Database connection pool exhausted during peak traffic",
        "description": "Worker nodes throwing connection pool timeout exception at 14:00 daily peak.",
        "priority": "Urgent",
        "status": "In Progress",
        "assignee": "Elena Rostova",
        "customer": "Soylent Corp (ops@soylent.com)",
        "created_at": "2026-08-28T05:15:00Z",
        "tags": ["database", "infra", "critical"],
        "channel": "Chat"
      },
      {
        "id": "TICK-1015",
        "title": "Cannot delete custom webhook configuration",
        "description": "Clicking delete icon on webhook management modal does not fire API call and no error is logged.",
        "priority": "Medium",
        "status": "Open",
        "assignee": "Alex Rivera",
        "customer": "Veidt Enterprises (adrian@veidt.com)",
        "created_at": "2026-08-28T08:45:00Z",
        "tags": ["webhook", "settings"],
        "channel": "Portal"
      },
      {
        "id": "TICK-1016",
        "title": "SSO login redirect loop on Google Workspace",
        "description": "User cannot authenticate via Google Workspace; redirected in loop without error code.",
        "priority": "High",
        "status": "Open",
        "assignee": "Alex Rivera",
        "customer": "Globex Industries (helpdesk@globex.com)",
        "created_at": "2026-08-28T11:30:00Z",
        "tags": ["auth", "sso"],
        "channel": "Email"
      },
      {
        "id": "TICK-1017",
        "title": "Two-factor authentication SMS delays in UK region",
        "description": "Verification SMS codes are arriving 15 to 20 minutes late for +44 mobile numbers.",
        "priority": "High",
        "status": "In Progress",
        "assignee": "Sarah Chen",
        "customer": "Tyrell Corp (security@tyrell.co.uk)",
        "created_at": "2026-08-28T09:10:00Z",
        "tags": ["2fa", "sms", "uk"],
        "channel": "Phone"
      },
      {
        "id": "TICK-1018",
        "title": "Need documentation on SCIM provisioning integration",
        "description": "Looking for setup guide and sample payloads for Azure AD SCIM user provisioning.",
        "priority": "Low",
        "status": "Closed",
        "assignee": "David Kim",
        "customer": "Aperture Science (glados@aperture.com)",
        "created_at": "2026-08-25T15:00:00Z",
        "tags": ["scim", "azure", "docs"],
        "channel": "Portal"
      },
      {
        "id": "TICK-1019",
        "title": "Billing invoice generated with incorrect VAT rate",
        "description": "Our invoice #INV-8921 shows 22% VAT instead of 20% for UK business tax exemption.",
        "priority": "Medium",
        "status": "Open",
        "assignee": "Sarah Chen",
        "customer": "Weyland-Yutani (finance@weyland.com)",
        "created_at": "2026-08-28T10:15:00Z",
        "tags": ["billing", "tax", "vat"],
        "channel": "Email"
      },
      {
        "id": "TICK-1020",
        "title": "CSV import crashes when file contains emoji in notes",
        "description": "Import tool fails with character encoding error if customer feedback contains unicode emojis.",
        "priority": "Medium",
        "status": "Open",
        "assignee": "",
        "customer": "Omni Consumer Products (qa@ocp.com)",
        "created_at": "2026-08-28T11:45:00Z",
        "tags": ["import", "encoding", "csv"],
        "channel": "Portal"
      }
    ]
  },
  issues: {
    name: "Edge Cases, Duplicates & Malformed Records (CSV)",
    description: "Includes unparseable lines, empty rows, missing fields that are safely recovered, and exact duplicates.",
    format: "csv",
    rawCsv: `id,title,description,priority,status,assignee,customer,created_at,tags,channel
TICK-2001,Production database replication lag spike,"Replication lag increased past 45 seconds on replica-east-02.",Urgent,Open,Sarah Chen,Acme Corp,2026-08-29T10:00:00Z,database,Alert
TICK-2002,Production database replication lag spike,"Replication lag increased past 45 seconds on replica-east-02.",Urgent,Open,Sarah Chen,Acme Corp,2026-08-29T10:02:00Z,database,Email
TICK-2003,User unable to download CSV reports,"Reports page shows spinner indefinitely when clicking Download CSV.",,Open,Alex Rivera,Globex,2026-08-29T10:15:00Z,reporting,Portal
TICK-2004,Missing billing address on subscription renewal,"Need to add PO box to account invoice.",Low,,David Kim,Initech,2026-08-29T10:20:00Z,billing,Email
TICK-2005,Broken image links in knowledge base articles,"Screenshots in article KB-402 return 404.",Medium,Pending,,Hooli,2026-08-29T10:30:00Z,docs,Portal

,,,,,,,,
TICK-2006,,"Empty title ticket with description only.",High,Open,Alex Rivera,Umbrella,2026-08-29T10:45:00Z,,Chat
,Missing ID completely,"This ticket has no ID at all, but has a title and description.",Medium,Open,,Cyberdyne,2026-08-29T11:00:00Z,test,Email
TICK-2007,Broken unclosed quote record,"This description starts a quote that does not end properly,High,Open,David Kim,Stark,2026-08-29T11:10:00Z
TICK-2008,Extra columns row,"Normal description",Low,Resolved,Elena Rostova,Wayne,2026-08-29T11:20:00Z,extra1,extra2,extra3,extra4
TICK-2009,Valid ticket after corrupted lines,"Ensuring parser recovers gracefully and continues processing downstream tickets.",High,Open,Sarah Chen,Pied Piper,2026-08-29T11:30:00Z,recovery,Portal`
  }
};
