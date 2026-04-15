-- Quinn QA knowledge base schema

-- QA Reports: verification sessions and their results
CREATE TABLE IF NOT EXISTS qa_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,                   -- report title
    summary TEXT NOT NULL,                 -- brief summary
    app_name TEXT,                         -- which app was verified
    severity TEXT DEFAULT 'info',          -- info/low/medium/high/critical
    findings TEXT,                         -- JSON array of findings
    created_at TEXT NOT NULL
);

-- Bug Patterns: recurring issues for regression detection
CREATE TABLE IF NOT EXISTS bug_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    app_name TEXT,
    severity TEXT,                         -- CRITICAL/HIGH/MEDIUM/LOW
    category TEXT,                         -- wiring/contract/integration/visual/performance
    pattern TEXT,                          -- what to look for (grep pattern, endpoint, etc.)
    occurrences INTEGER DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    resolved INTEGER DEFAULT 0,
    resolution TEXT,                       -- how it was fixed (for future reference)
    related_features TEXT                  -- JSON array of feature IDs for cross-feature clustering
);

-- Release Notes: generated changelogs
CREATE TABLE IF NOT EXISTS release_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name TEXT NOT NULL,
    version TEXT NOT NULL,
    title TEXT,
    content TEXT NOT NULL,                 -- full release notes markdown
    features_count INTEGER DEFAULT 0,
    fixes_count INTEGER DEFAULT 0,
    breaking_changes TEXT,                 -- JSON array
    published INTEGER DEFAULT 0,
    published_at TEXT,
    created_at TEXT NOT NULL
);

-- Triage Log: issue/bug triage decisions
CREATE TABLE IF NOT EXISTS triage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,                  -- github_issue/board_feature/discord_report
    source_id TEXT NOT NULL,               -- issue number, feature ID, message ID
    app_name TEXT,
    classification TEXT NOT NULL,          -- already_fixed/actionable/stale/duplicate
    severity TEXT,                         -- CRITICAL/HIGH/MEDIUM/LOW
    reason TEXT,                           -- explanation of classification
    action_taken TEXT,                     -- closed/labeled/commented/escalated
    created_at TEXT NOT NULL
);

-- Apps: tracked applications in the portfolio
CREATE TABLE IF NOT EXISTS apps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    github_repo TEXT,
    server_url TEXT,
    last_checked_at TEXT,
    config TEXT                            -- JSON config
);

-- Regression Tests: scripted checks tied to known bug patterns
CREATE TABLE IF NOT EXISTS regression_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    steps TEXT,                            -- JSON array of steps
    expected_result TEXT,
    related_bug TEXT,                      -- bug pattern title or id
    app_name TEXT,
    last_run TEXT,
    last_result TEXT DEFAULT 'pending',    -- pending/pass/fail
    created_at TEXT NOT NULL
);
