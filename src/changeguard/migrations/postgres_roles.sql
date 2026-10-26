-- Least-privilege roles for PostgreSQL. Applied by `changeguard init-db` (as the admin role).
-- Passwords are substituted from CG_PG_*_PASSWORD env vars (dev defaults exist: change them in production).
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cg_reader')   THEN CREATE ROLE cg_reader   LOGIN PASSWORD '{{READER_PW}}';   END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cg_recorder') THEN CREATE ROLE cg_recorder LOGIN PASSWORD '{{RECORDER_PW}}'; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cg_sandbox')  THEN CREATE ROLE cg_sandbox  LOGIN PASSWORD '{{SANDBOX_PW}}';  END IF;
END $$;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM cg_reader, cg_recorder, cg_sandbox;
GRANT USAGE ON SCHEMA public TO cg_reader, cg_recorder, cg_sandbox;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO cg_reader, cg_recorder, cg_sandbox;

-- Recorder: workflow state, traces, approvals, audit. Cannot touch business data.
GRANT INSERT, UPDATE ON engineering_changes, agent_runs, proposed_actions TO cg_recorder;
GRANT INSERT ON agent_steps, approvals, audit_events TO cg_recorder;

-- Sandbox writer: the only role that may change sandbox business state.
GRANT INSERT ON change_orders, audit_events TO cg_sandbox;
GRANT INSERT, UPDATE ON executed_actions TO cg_sandbox;
GRANT UPDATE ON proposed_actions TO cg_sandbox;
GRANT UPDATE (reserved) ON inventory TO cg_sandbox;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO cg_recorder, cg_sandbox;
