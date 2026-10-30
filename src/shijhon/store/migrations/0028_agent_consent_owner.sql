-- Bind the consent step to the account verified in this short-lived browser flow.
ALTER TABLE agent_oauth_flows ADD COLUMN owner TEXT NOT NULL DEFAULT '';
