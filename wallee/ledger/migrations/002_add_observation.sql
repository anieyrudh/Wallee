-- Add observation column to actions table.
-- The LLM's observation field was being lost before persistence.

ALTER TABLE actions ADD COLUMN observation TEXT DEFAULT '';
