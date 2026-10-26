-- Audit records cannot silently disappear or change (PostgreSQL).
CREATE FUNCTION cg_block_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_events_no_update BEFORE UPDATE OR DELETE ON audit_events
  FOR EACH ROW EXECUTE FUNCTION cg_block_mutation();
CREATE TRIGGER approvals_no_update BEFORE UPDATE OR DELETE ON approvals
  FOR EACH ROW EXECUTE FUNCTION cg_block_mutation();
