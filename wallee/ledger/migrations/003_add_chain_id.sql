-- Add chain_id and chain_seq columns for ACTION_CHAIN support.
-- chain_id groups actions in a chain, chain_seq orders them.

ALTER TABLE actions ADD COLUMN chain_id TEXT DEFAULT NULL;
ALTER TABLE actions ADD COLUMN chain_seq INTEGER DEFAULT NULL;
