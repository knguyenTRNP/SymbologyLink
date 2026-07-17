# Security Policy

## Supported version

Security fixes are applied to the latest release on the default branch.

## Reporting a vulnerability

Use the repository's private vulnerability-reporting feature when available. Do not publish credentials, customer data, exploit details, or sensitive provider responses in a public issue.

Include the affected component, reproduction steps, expected impact, and any suggested mitigation. Reports are evaluated before public disclosure.

## Deployment guidance

- Configure `SYMBOLOGYLINK_API_KEY` before exposing the API outside a trusted network.
- Terminate TLS at a reverse proxy.
- Mount provider credentials through environment variables or a secret manager.
- Keep uploaded datasets, caches, job databases, rules, and overrides outside the source tree in production.
