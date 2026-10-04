---
id: externalize-public-keys
version: 1.0.0
title: Embedded public keys -> configuration
tier: security
# Public keys are not secret. This pack is in scope only when the user chooses
# "Scrub public keys" after the repository scan; otherwise the keys are listed
# in the pull request and left as they are.
detect:
  any:
    - secret_scan: public
applies_to:
  - secret_scan: public
include_tests: true
context: none
depends_on: []
decisions: []
eliminates: []
acceptance:
  - no_secrets: public
---

## transform

You are moving embedded public keys (PEM `-----BEGIN PUBLIC KEY-----` blocks and base64 X.509
keys such as `MIIBIjAN…`) out of the source into configuration, so they can be rotated without a
code change.

### Java

```java
// before
private static final String PUBLIC_KEY = "MIIBIjAN...";

// after
private static final String PUBLIC_KEY = requiredEnv("JWT_PUBLIC_KEY");
```

Use the same fail-fast `requiredEnv` helper as `externalize-secrets` (add it once per module).
For a key that is large or shared by several services, read it from a resource or a file path
instead: `PUBLIC_KEY_PATH` → `Files.readString(Path.of(requiredEnv("PUBLIC_KEY_PATH")))`.
Keep the parsing code (`KeyFactory`, `X509EncodedKeySpec`) unchanged; only the source of the
string changes.

### Tests

Tests generate a key pair (`KeyPairGenerator.getInstance("RSA")`) instead of embedding one.

### Pull request

List each variable under "Configuration required" with the value to set: the same public key
that was embedded (it is public, so it may be shown). No rotation is needed.
