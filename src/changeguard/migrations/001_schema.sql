-- ChangeGuard schema (dialect-neutral). {{AUTOINC_PK}} is substituted per backend.
-- Dates are ISO-8601 TEXT written by application code (UTC). JSON is stored as TEXT.
-- Booleans are INTEGER 0/1 so both backends behave identically.

CREATE TABLE sites (
  site_id TEXT PRIMARY KEY,
  name    TEXT NOT NULL
);

CREATE TABLE components (
  component_id     TEXT PRIMARY KEY,
  description      TEXT NOT NULL,
  kind             TEXT NOT NULL CHECK (kind IN ('purchased','assembly','product')),
  lifecycle_status TEXT NOT NULL CHECK (lifecycle_status IN ('active','discontinued','last_time_buy')),
  criticality      INTEGER CHECK (criticality IS NULL OR (criticality BETWEEN 1 AND 5)),
  uom              TEXT NOT NULL DEFAULT 'ea'
);

CREATE TABLE bom_links (
  parent_id TEXT NOT NULL REFERENCES components(component_id),
  child_id  TEXT NOT NULL REFERENCES components(component_id),
  qty_per   INTEGER NOT NULL CHECK (qty_per > 0),
  PRIMARY KEY (parent_id, child_id),
  CHECK (parent_id <> child_id)
);
CREATE INDEX idx_bom_child ON bom_links(child_id);

CREATE TABLE component_alternates (
  component_id TEXT NOT NULL REFERENCES components(component_id),
  alternate_id TEXT NOT NULL REFERENCES components(component_id),
  relationship TEXT NOT NULL DEFAULT 'declared_candidate' CHECK (relationship IN ('declared_candidate')),
  PRIMARY KEY (component_id, alternate_id),
  CHECK (component_id <> alternate_id)
);

CREATE TABLE suppliers (
  supplier_id TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  status      TEXT NOT NULL CHECK (status IN ('active','inactive','on_hold')),
  risk_level  TEXT CHECK (risk_level IS NULL OR risk_level IN ('low','medium','high'))
);

CREATE TABLE component_suppliers (
  component_id    TEXT NOT NULL REFERENCES components(component_id),
  supplier_id     TEXT NOT NULL REFERENCES suppliers(supplier_id),
  lead_time_days  INTEGER CHECK (lead_time_days IS NULL OR lead_time_days >= 0),
  unit_cost_cents INTEGER CHECK (unit_cost_cents IS NULL OR unit_cost_cents >= 0),
  is_preferred    INTEGER NOT NULL DEFAULT 0 CHECK (is_preferred IN (0,1)),
  PRIMARY KEY (component_id, supplier_id)
);

CREATE TABLE inventory (
  component_id TEXT NOT NULL REFERENCES components(component_id),
  site_id      TEXT NOT NULL REFERENCES sites(site_id),
  on_hand      INTEGER NOT NULL CHECK (on_hand >= 0),
  reserved     INTEGER NOT NULL CHECK (reserved >= 0),
  PRIMARY KEY (component_id, site_id)
);

CREATE TABLE demand_plan (
  product_id    TEXT NOT NULL REFERENCES components(component_id),
  site_id       TEXT NOT NULL REFERENCES sites(site_id),
  planned_units INTEGER NOT NULL CHECK (planned_units >= 0),
  horizon_days  INTEGER NOT NULL CHECK (horizon_days > 0),
  PRIMARY KEY (product_id, site_id)
);

CREATE TABLE compliance_requirements (
  requirement_id TEXT PRIMARY KEY,
  market         TEXT NOT NULL,
  regulation     TEXT NOT NULL,
  description    TEXT NOT NULL
);

CREATE TABLE compliance_records (
  record_id      TEXT PRIMARY KEY,
  component_id   TEXT NOT NULL REFERENCES components(component_id),
  requirement_id TEXT NOT NULL REFERENCES compliance_requirements(requirement_id),
  status         TEXT NOT NULL CHECK (status IN ('confirmed','failed','unresolved')),
  evidence_ref   TEXT,
  evidence_date  TEXT,
  note           TEXT
);
CREATE INDEX idx_compliance_component ON compliance_records(component_id);

CREATE TABLE documents (
  doc_id   TEXT PRIMARY KEY,
  title    TEXT NOT NULL,
  doc_type TEXT NOT NULL CHECK (doc_type IN ('standard','spec','memo','supplier_notice')),
  body     TEXT NOT NULL
);

CREATE TABLE engineering_changes (
  change_id        TEXT PRIMARY KEY,
  component_id     TEXT NOT NULL REFERENCES components(component_id),
  change_type      TEXT NOT NULL,
  description      TEXT NOT NULL,
  urgency          TEXT NOT NULL,
  target_market    TEXT,
  constraints_json TEXT NOT NULL,
  status           TEXT NOT NULL CHECK (status IN ('submitted','investigating','investigated','escalated','closed')),
  idempotency_key  TEXT NOT NULL UNIQUE,
  requested_by     TEXT NOT NULL,
  created_at       TEXT NOT NULL
);

CREATE TABLE agent_runs (
  run_id        TEXT PRIMARY KEY,
  change_id     TEXT NOT NULL REFERENCES engineering_changes(change_id),
  runner        TEXT NOT NULL CHECK (runner IN ('agent','baseline')),
  provider      TEXT,
  model         TEXT,
  status        TEXT NOT NULL,
  stop_reason   TEXT,
  started_at    TEXT NOT NULL,
  ended_at      TEXT,
  step_count    INTEGER NOT NULL DEFAULT 0,
  input_tokens  INTEGER,
  output_tokens INTEGER,
  cost_usd      REAL,
  report_json   TEXT,
  config_json   TEXT NOT NULL
);

CREATE TABLE agent_steps (
  run_id        TEXT NOT NULL REFERENCES agent_runs(run_id),
  idx           INTEGER NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('llm','tool','guard','system')),
  name          TEXT NOT NULL,
  attempt       INTEGER NOT NULL DEFAULT 1,
  input_json    TEXT,
  output_json   TEXT,
  status        TEXT NOT NULL CHECK (status IN ('ok','error','blocked','retry')),
  error_kind    TEXT,
  error_message TEXT,
  started_at    TEXT NOT NULL,
  duration_ms   INTEGER NOT NULL DEFAULT 0,
  input_tokens  INTEGER,
  output_tokens INTEGER,
  PRIMARY KEY (run_id, idx)
);

CREATE TABLE proposed_actions (
  action_id    TEXT PRIMARY KEY,
  change_id    TEXT NOT NULL REFERENCES engineering_changes(change_id),
  run_id       TEXT REFERENCES agent_runs(run_id),
  action_type  TEXT NOT NULL CHECK (action_type IN ('CREATE_CHANGE_ORDER','RESERVE_STOCK_HOLD')),
  payload_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  status       TEXT NOT NULL CHECK (status IN
    ('proposed','approved','rejected','revision_requested','superseded','executed','failed','verified','verification_failed')),
  supersedes   TEXT,
  created_at   TEXT NOT NULL
);

CREATE TABLE approvals (
  approval_id  TEXT PRIMARY KEY,
  action_id    TEXT NOT NULL REFERENCES proposed_actions(action_id),
  payload_hash TEXT NOT NULL,
  decision     TEXT NOT NULL CHECK (decision IN ('approved','rejected','revision_requested')),
  reviewer     TEXT NOT NULL,
  role         TEXT NOT NULL,
  reason       TEXT,
  decided_at   TEXT NOT NULL
);

CREATE TABLE executed_actions (
  exec_id             TEXT PRIMARY KEY,
  action_id           TEXT NOT NULL UNIQUE REFERENCES proposed_actions(action_id),
  approval_id         TEXT NOT NULL UNIQUE REFERENCES approvals(approval_id),
  payload_hash        TEXT NOT NULL,
  before_json         TEXT NOT NULL,
  after_json          TEXT NOT NULL,
  outcome             TEXT NOT NULL CHECK (outcome IN ('applied','failed')),
  error               TEXT,
  executed_at         TEXT NOT NULL,
  verification_status TEXT NOT NULL CHECK (verification_status IN ('pending','passed','failed')),
  verification_detail TEXT
);

CREATE TABLE change_orders (
  co_id                    TEXT PRIMARY KEY,
  change_id                TEXT NOT NULL REFERENCES engineering_changes(change_id),
  component_id             TEXT NOT NULL REFERENCES components(component_id),
  replacement_component_id TEXT REFERENCES components(component_id),
  disposition              TEXT NOT NULL CHECK (disposition IN
    ('substitute_candidate_review','last_time_buy','redesign_review')),
  affected_assemblies_json TEXT NOT NULL,
  notes                    TEXT,
  status                   TEXT NOT NULL CHECK (status IN ('draft')),
  payload_hash             TEXT NOT NULL UNIQUE,
  created_at               TEXT NOT NULL
);

CREATE TABLE audit_events (
  seq         {{AUTOINC_PK}},
  ts          TEXT NOT NULL,
  actor       TEXT NOT NULL,
  event_type  TEXT NOT NULL,
  change_id   TEXT,
  run_id      TEXT,
  action_id   TEXT,
  detail_json TEXT NOT NULL
);
CREATE INDEX idx_audit_change ON audit_events(change_id);

CREATE TABLE schema_migrations (
  version    TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL
);
