# Internal contracts and formats

- Keep one current implementation with permanent, unversioned identifiers for
  internal contracts, schemas, formats and processing rules.
- Keep identifiers when schemas change. Do not add version fields, numeric version
  suffixes, iteration names, migrations or legacy aliases.
- Preserve structure, identity, hash and publication checks.
- Preserve versions of external tools, protocols, models and parsers, and document
  revision numbers.

Permanent format names: `evidence-ledger`, `architecture-artifacts`,
`compact-context`, `dictionary`, `tool-observer`, `tool-observer-replay`.
