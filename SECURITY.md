# Security policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository. Do not open a public issue
and do not include production customer data. Include affected versions, reproduction steps,
impact and a minimal proof of concept when possible.

You can expect an initial acknowledgement within seven days. A fix and disclosure timeline
will depend on severity and reproducibility.

## Supported versions

Security fixes are applied to the latest release and the current default branch. Older
releases may require upgrading before a fix can be provided.

## Deployment responsibility

Production deployments must use independent random values for every security key, canonical
HTTPS URLs, a TLS reverse proxy, and an internal-only database network. Rotate all credentials
after suspected exposure and rebuild images without cached layers. Keep the database and
persisted application files backed up and restrict access to `.env`.
