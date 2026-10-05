# Security policy

TwinSpark Manager runs as root helpers and services on your DGX Sparks. Please report security problems
privately, not in a public issue.

**How to report:** use GitHub's private vulnerability reporting. On the repository page, open
*Security* → *Report a vulnerability*. Include:

- the version (`tsm --version`)
- what you did
- what happened
- what an attacker would need: network position, keys, local access

You should get a first answer within a week. This is a spare-time project, so a fix can take longer.
When the fix is released, it is credited in the [changelog](CHANGELOG.md) unless you prefer otherwise.

**What is covered:** only the latest release on `main`.

**Before you report:** read the security model and the known limits in [docs/security.md](docs/security.md).
For example, the service user being effectively root through the `docker` group is a documented limit,
not a new finding.
