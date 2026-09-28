# Agent Relay

Agent Relay is a small FastAPI service for registering agents, delivering one
task at a time, and recording results. PostgreSQL persists the queue and
attempts, while workers execute tasks on their own machines. The included worker deterministically returns `input.upper()`.

## Run it

Start the API and PostgreSQL together:

```bash
docker compose up -d --build
```

Open <http://127.0.0.1:8000/> for the token-based local dashboard. Compose runs
the `postgres` service (data in the `postgres-data` volume) and points the API
at it through `RELAY_DATABASE_URL`. To run the API on the host instead, keep the
`postgres` service up and run `uv sync && uv run uvicorn main:app --reload`; the
default URL is `postgresql+psycopg://relay:relay@localhost:5432/agent_relay`. `GET /health` is a liveness check and `GET /ready` verifies database
connectivity and schema (it queries the real tables, so a wiped volume
reports not-ready instead of passing with zero tables).

Register two identities and send a task:

```bash
alice=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"alice"}')
bob=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"uppercase"}')
```

The response contains each agent's secret `token` once. Keep it outside source
control. Use `Authorization: Bearer <token>` for all subsequent API calls;
registration is the only unauthenticated endpoint. For a shared installation,
set `RELAY_ENROLLMENT_SECRET` and send it as `X-Enrollment-Secret` when
registering.

## Run the deterministic worker

The worker can register itself and save credentials in a mode-0600 JSON file:

```bash
uv run python main.py worker \
  --base-url http://127.0.0.1:8000 \
  --name uppercase \
  --credentials ./uppercase-credentials.json \
  --worker-id laptop-1
```

For failure/redelivery demonstrations, make local execution intentionally slow
and stop the process after one completion:

```bash
uv run python main.py worker --credentials ./uppercase-credentials.json \
  --slow-seconds 75 --worker-id slow-laptop
```

The worker heartbeats during long work. Killing it leaves the claim leased;
after the 60-second lease expires, another worker can claim the task with a new
token and incremented attempt number. `RELAY_LEASE_SECONDS` and
`RELAY_MAX_ATTEMPTS` are configurable server settings.

An existing credential can also be supplied explicitly (the token is not
written to disk):

```bash
uv run python main.py worker --agent-id agent_123 --token agt_… --worker-id laptop-2
```

## Storage and delivery behavior

`database.py` contains the SQLAlchemy models, the PostgreSQL engine, and the
`transaction()` helper. `storage.py` contains task/claim/recovery operations;
routes and request models are kept in `main.py` and `schemas.py`. Claims select
the next queued task with `FOR UPDATE SKIP LOCKED`, so concurrent workers each
get a different task without blocking one another; heartbeat, terminal
submission, and recovery lock the task row before its attempts.

Claims are at-least-once and leased for 60 seconds by default. Heartbeats extend
an active lease. A completion or failure must include the recipient's bearer
token and claim token. Repeating the exact terminal request with that claim
token is idempotent; a stale token or different result receives `409`.

## Verify

The test suite covers the main protocol, sender/recipient access boundaries,
hashed claim-token behavior, idempotent terminal retries, concurrent claims,
lease expiry before and after recovery, pagination/error shape, and dashboard
asset serving:

```bash
uv run pytest -q
```

`test_integration.py` is an end-to-end check against the running stack: two
agents exchange a task and its result over HTTP, then the test reads the row
back from the app's `agent_relay` database. It only adds rows and skips if the
API is not up at `RELAY_BASE_URL` (default `http://localhost:8000`).

Tests need the compose `postgres` service running. They default to the
scratch `agent_relay_test` database (created on first start by
`docker/postgres-init/`) so they don't reset the app's `agent_relay` data. The
fixture drops and recreates all tables on whatever `RELAY_DATABASE_URL` points
at, so never point it at a database with data you need.
