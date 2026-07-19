# Contributing

Thanks for considering contributing to ResolveOps.

## Development setup

```powershell
Copy-Item .env.example .env
docker compose up -d --build
python -m pytest -q
```

For isolated container tests:

```powershell
docker compose --profile test run --rm test
```

## Design rules

Keep these boundaries intact:

- CLI calls ResolveOps API only.
- LLM can call read tools only during investigation.
- Write tools are proposed as Action Plans and executed only through Policy, Approval, Executor and Verifier.
- ERPNext is an adapter target, not the Agent architecture itself.
- Do not add direct ERP writes outside Executor or controlled fault-injection boundaries.
- Do not put secrets, local `.env`, local DB files or customer data into git.

## Pull request expectations

Each change should include:

- clear explanation of the Agent/runtime boundary affected;
- tests for new behavior;
- documentation update if the user-facing behavior changes.

Before submitting:

```powershell
python -m pytest -q
docker compose --profile test run --rm test
```

## Commit style

Use concise, imperative commit messages, for example:

```text
Add controlled fault injection API
Document production readiness boundary
```
