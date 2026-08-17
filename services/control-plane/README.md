# Registration control plane (phase 1)

This directory is the first production-oriented foundation for the long-term
registration architecture. It intentionally contains only the control-plane
health/readiness surface and the durable PostgreSQL schema; the existing
Python browser workflow remains the worker implementation for the next phase.

## Local startup

From the repository root:

```sh
docker compose -f docker-compose.platform.yml up --build -d
curl -fsS http://127.0.0.1:8080/healthz
curl -fsS http://127.0.0.1:8080/readyz
```

The control plane applies the embedded migration on startup. PostgreSQL and
Redis data live in named Docker volumes. To stop the stack while preserving
data:

```sh
docker compose -f docker-compose.platform.yml down
```

To reset local data as well:

```sh
docker compose -f docker-compose.platform.yml down -v
```

The default credentials and ports are for local development only. Set
`PLATFORM_POSTGRES_PASSWORD`, `PLATFORM_POSTGRES_USER`,
`PLATFORM_POSTGRES_DB`, `PLATFORM_POSTGRES_PORT`, `PLATFORM_REDIS_PORT`, and
`CONTROL_PLANE_PORT` in the shell or a local environment file before sharing
the stack.

## Endpoints

- `GET /healthz`: process liveness; does not require PostgreSQL or Redis.
- `GET /readyz`: PostgreSQL connectivity, required schema, and Redis PING.
- `GET /`: service/version and uptime metadata.

Readiness returns `503` with per-dependency status until all checks pass. Error
details are kept in server logs rather than returned to callers.

## Configuration

The control plane reads the following environment variables (see
`.env.example`):

| Variable | Default | Purpose |
| --- | --- | --- |
| `CONTROL_PLANE_HTTP_ADDR` | `:8080` | HTTP bind address |
| `DATABASE_URL` | local PostgreSQL URL | PostgreSQL connection string |
| `REDIS_URL` | local Redis URL | Redis connection string |
| `READINESS_TIMEOUT` | `2s` | Per-readiness request budget |
| `STARTUP_TIMEOUT` | `45s` | PostgreSQL/migration startup budget |
| `SHUTDOWN_TIMEOUT` | `10s` | Graceful HTTP shutdown budget |
| `AUTO_MIGRATE` | `true` | Apply embedded migrations at startup |
| `DB_MAX_OPEN_CONNS` | `25` | Database pool upper bound |
| `DB_MAX_IDLE_CONNS` | `10` | Database idle pool bound |
| `DB_CONN_MAX_LIFETIME` | `30m` | Database connection lifetime |

## Schema

`internal/migrate/migrations/000001_platform.sql` creates:

- `accounts`
- `registration_batches`
- `registration_jobs`
- `job_events`
- `workers`
- `platform_schema_migrations`

Indexes cover status queues, batch pagination, worker leases, event sequence
reads, and terminal-job retention. Account credentials are represented as an
encrypted blob plus a key reference; raw tokens should not be placed in log
messages or JSON metadata.

## Development checks

```sh
cd services/control-plane
gofmt -w cmd internal
go test ./...
```

The next integration phase should connect the Python worker to a durable job
claim protocol and publish sequenced `job_events`; the schema already supports
cursor-based reads without loading the complete registration history.
