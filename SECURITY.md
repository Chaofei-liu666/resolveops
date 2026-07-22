# Security Policy

ResolveOps is a sandbox-ready Agent project, not a certified production ERP automation product.

## Supported usage

Current supported usage:

- local development;
- technical project review;
- ERPNext sandbox testing;
- controlled staging experiments.

Do not grant this project unrestricted access to a real production ERP environment without additional enterprise controls.

## Secrets

Never commit:

- `.env`;
- ERPNext API key or secret;
- LLM API key;
- operator key;
- database dump;
- private key;
- production customer or order data.

Use `.env.example` as a template only.

## Fault injection

Fault injection can intentionally change ERPNext sandbox data. It must remain disabled in production:

```text
ENABLE_FAULT_INJECTION=false
```

The API also rejects fault injection when:

```text
APP_ENV=production
```

## Reporting security issues

If this repository is published publicly, please report security issues through a private GitHub security advisory if enabled, or by contacting the repository owner directly.

Do not open public issues containing secrets, customer data, exploit details, or live credentials.

## Production hardening checklist

Before real production write access, add:

- enterprise IAM / SSO;
- secret manager;
- audit log retention policy;
- production monitoring and alerting;
- backup and restore verification;
- ERP permission scoping;
- load and concurrency tests;
- incident runbook;
- manual rollback procedure.
