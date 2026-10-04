---
id: externalize-secrets
version: 1.0.0
title: Hardcoded secrets -> environment variables
tier: security
# Evaluated by forge's own secret scanner (python/guardrails.py), so the patterns
# live in one place: private and symmetric keys (AES, HMAC, SecretKeySpec
# literals, byte-array keys), passwords, API keys and tokens.
detect:
  any:
    - secret_scan: secret
applies_to:
  - secret_scan: secret
include_tests: true
context: none
depends_on: []
decisions: []
eliminates: []
acceptance:
  # No hardcoded secret may remain anywhere in the repository.
  - no_secrets: secret
---

## transform

You are removing hardcoded secrets from the code. This item is ALWAYS in scope when it applies.
The secret VALUE must never be copied anywhere: not into another file, a test, a comment, a
commit message, the chat or the pull request. You replace it with a lookup and the deployer
supplies the value.

Name each variable by purpose, upper snake case: `AES_KEY`, `HMAC_SIGNING_KEY`,
`SETTLEMENT_API_KEY`, `GATEWAY_PASSWORD`, `KEYSTORE_PASSWORD`. Keep a list of every name you
introduce; it goes into the PR under "Configuration required".

### Java

A string constant becomes an environment lookup that fails fast when the variable is missing:

```java
// before
private static final String AES_KEY = "<literal>";

// after
private static final String AES_KEY = requiredEnv("AES_KEY");

private static String requiredEnv(String name) {
    String value = System.getenv(name);
    if (value == null || value.isBlank()) {
        throw new IllegalStateException("Missing required environment variable " + name);
    }
    return value;
}
```

In a Spring bean, prefer injection: `@Value("${AES_KEY}") private String aesKey;`.

`new SecretKeySpec("<literal>".getBytes(), "AES")` becomes
`new SecretKeySpec(requiredEnv("AES_KEY").getBytes(StandardCharsets.UTF_8), "AES")`, or, when the
key is binary, decode it: `HexFormat.of().parseHex(requiredEnv("AES_KEY"))` (hex) or
`Base64.getDecoder().decode(requiredEnv("AES_KEY"))` (base64).

A byte-array literal (`byte[] key = {0x01, 0x02, ...}`, `byte[] IV = new byte[]{...}`) becomes
`HexFormat.of().parseHex(requiredEnv("AES_KEY"))`. Never write the bytes as hex into an
environment file in the repository.

A PEM private key in a string becomes a file path or a secret-manager reference read at
startup (`PRIVATE_KEY_PEM` environment variable, or `PRIVATE_KEY_PATH` pointing to a file mounted
at runtime).

### Configuration files

- `.properties` / `.yml`: `aes.key=${AES_KEY}`.
- Liberty `server.xml`: `password="${env.KEYSTORE_PASSWORD}"`; a `<variable name="…password"
  defaultValue="literal"/>` loses the literal default (`defaultValue=""` or remove the default).
- Tomcat `context.xml`: `password="${DB_PASSWORD}"` with the value passed as a system property.

### Tests

A test that needs a key generates one (`KeyGenerator.getInstance("AES").generateKey()`) or
reads a test-only variable; it never copies the original value.

### Pull request

Under "Configuration required" list every variable name and where it is read. Add: "The
original values were committed to the repository history and must be rotated."
