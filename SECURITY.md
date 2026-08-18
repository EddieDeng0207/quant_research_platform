# Security Policy

## Supported versions

Security fixes are applied to the latest `main` branch. Historical tags and
research artifacts are immutable evidence and are not patched in place; a fix
that changes their interpretation produces a new version.

## Reporting a vulnerability

Use GitHub's **Security → Report a vulnerability** private reporting form for
credential exposure, dependency compromise, path traversal, artifact tampering,
unsafe deserialization, or another security issue.

Do not open a public issue containing exploit details, credentials, private data,
or provider-licensed data. If private vulnerability reporting is temporarily
unavailable, contact the maintainer through the GitHub profile without including
sensitive details and request a private channel.

Please include:

- the affected commit, module, or artifact schema;
- minimal reproduction steps using synthetic data where possible;
- expected impact and whether credentials or licensed data may be involved;
- any temporary mitigation already applied.

The maintainer will acknowledge a complete report as soon as practical, validate
the impact, rotate any affected credential first, and coordinate disclosure after
a fix is available.

## Data and research safety

The repository must not contain API tokens, `.env` files, broker account data, or
provider-licensed raw datasets. A research result that exposes a security or data
governance defect is treated as invalid until a new clean artifact is generated.
