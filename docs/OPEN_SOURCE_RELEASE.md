# Open-source release gate

The repository stays private until every verification item below is complete. Changing
repository visibility is a separate, final operation and is intentionally not part of the
build or deployment workflows.

## Current source tree

- [x] Hosted-service billing, checkout and operator administration are outside the core.
- [x] The standalone application starts without an extension package.
- [x] Hosted-service schema revisions and tests are outside the public migration chain.
- [x] Runtime configuration documents self-hosted defaults only.
- [x] Internal deployment scripts, generated audits and customer fixtures are removed.
- [x] Installation, architecture, contribution and security documentation is present.
- [x] Docker Compose exposes the application only on loopback and does not publish MariaDB.
- [x] Bootstrap requires a one-time token and creates the first account through `/setup`.

## Required verification

- [x] Full Python and browser test suite passes from the release tree.
- [x] Fresh Docker Compose installation migrates an empty MariaDB database.
- [x] Setup, logout and login work on the fresh stack.
- [x] HTML PDF rendering works inside the read-only application container.
- [x] Dependency audit reports no known vulnerabilities.
- [x] Secret and private-infrastructure scans report no release blockers.
- [x] GitHub CI and security workflows are green for the release commit.

## Publication procedure

The existing private repository history must not become public, even through a force-push.
After the release commit is verified, create a new one-commit source snapshot with:

```bash
./tools/create_public_snapshot.sh /tmp/fakturek-public
```

The command exports tracked files only and reruns the boundary verifier with the single-root
history gate. Publish that snapshot to a brand-new GitHub repository. If the final public
repository must keep the `fakturek.cz` name, rename/archive the historical private repository
first and create a new repository under the released name. Keep hosted deployment history,
patches and extensions private. Change visibility and create the first tag only after CI passes
from the new repository.
