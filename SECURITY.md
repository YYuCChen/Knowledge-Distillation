# Security policy

## Reporting a vulnerability

Please do not open a public issue for credentials, private source material, account identifiers, cookies, or a suspected security vulnerability.

Use GitHub's private vulnerability reporting for this repository when available. If it is not available, contact the repository maintainer through the GitHub profile and include only the minimum reproducible details.

## Redaction rules

Before sharing logs, screenshots, databases, or source samples, remove:

- API keys, access tokens, cookies, passwords, and signing keys;
- personal source text, Obsidian paths, local database files, and browser profiles;
- private platform identifiers, message IDs, and account names;
- machine-specific absolute paths and temporary directory names.

The application is local-first, but it can send selected material to user-configured platforms and model providers. Treat those providers' credentials, logs, and source content as private.
