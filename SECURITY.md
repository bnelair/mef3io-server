# Security Policy

## Reporting a vulnerability

Please report security issues privately, **not** as a public GitHub issue. Use
GitHub's [private vulnerability reporting](https://github.com/bnelair/mef3io-server/security/advisories/new)
(Security → Report a vulnerability), or contact the maintainers directly.

Useful details: affected version, platform, a minimal reproduction, and the
impact you observed. We aim to acknowledge reports within a week.

## Scope

mef3io-server is a network service that opens MEF 3.0 files (parsed by
[mef3io](https://github.com/bnelair/mef3io)) and serves signal ranges over gRPC.
In scope:

- **Path handling.** The server resolves client-supplied file paths (and, in
  Docker, maps them under `/host_root`). Issues that let a request read outside
  the intended data mount are security bugs — deploy with a read-only
  (`:ro`) mount scoped to the data you intend to expose.
- **Denial of service** from crafted requests (e.g. inputs that cause unbounded
  memory or CPU use in the cache/decode paths).
- **Memory-safety issues in the read path** that originate in the decode layer
  should be reported to [mef3io](https://github.com/bnelair/mef3io/security);
  cross-link the report here if the server is how you hit it.

The gRPC endpoint is **unauthenticated and unencrypted by default**. Do not
expose it directly to untrusted networks; place it behind an authenticated,
TLS-terminating proxy or restrict it to a trusted network.

## Supported versions

Fixes land on the latest release line. Please upgrade to the newest version
(and the newest `mef3io`) before reporting.
