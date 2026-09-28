-- Scratch database for `uv run pytest`; the test fixture drops and recreates
-- its tables, so it must stay separate from the app's `agent_relay` database.
CREATE DATABASE agent_relay_test OWNER relay;
