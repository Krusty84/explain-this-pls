# Provenance

`openapi.json` is a JSON-preserving subset of the generated OpenAPI at upstream
OpenCode v1.2.27 (`4ee426ba549131c4903a71dfb6259200467aca81`), downloaded 2026-09-29:

https://raw.githubusercontent.com/anomalyco/opencode/v1.2.27/packages/sdk/openapi.json

Original complete file SHA-256:
`ef236d6647f0b462ac7fb03454ae1a75575d951ff75021d7af7b38c8c34a84dd`.
The subset retains `/global/health`, `/session`, `/session/{sessionID}`,
`/session/{sessionID}/abort`, `/session/{sessionID}/message`, `/session/status`,
and the transitive closure of referenced component schemas. JSON indentation
changed; schema values did not. Upstream MIT license is included.

`../fake_opencode.py` constructs protocol-shaped messages from these definitions;
they are synthetic fixtures, not recordings of a real model. Its labelled
`AUDIT_FAKE_CASE` cases intentionally violate the protocol. The abbreviated
session creation response carries only the ID consumed by the client; assistant
responses include required fields. Tests do not claim the upstream retryCount
setting works; production explicitly refuses that unsupported guarantee.
