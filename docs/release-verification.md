# 0.3.0 local release verification

Reverified on 2026-10-24 after the architecture cross-review.
Package: `industriagents-profibus-mcp`.

These are local build and installed-artifact results, not a registry release.
No tag, commit, push or publication was performed by this verification.

## Checks

- Full server regression suite: 246 passed.
- Separate mock suite: 26 passed.
- The full suite includes the final native outcome/configuration regressions and
  the shared audit and policy-file tests; no additional focused runs are added
  to the counts above.
- Ruff lint and formatting checks passed for source and tests.
- `uv lock --check` and contract/package-version checks passed.
- Three npm launcher tests passed.
- Fresh wheel, source distribution and npm tarball built successfully.
- Both installed distribution routes initialized over stdio outside the source
  checkout and listed 7 canonical tools with the exact reviewed schemas.
  Invalid mode calls returned MCP error envelopes. Smoke tests selected the
  exact manifest-version artifact filenames.
- `twine check --strict` passed for wheel and source distribution.
- Artifact inspection confirmed current packaged sources/contracts, matching
  npm launcher bytes, and identical wheel bytes inside the npm archive.
- `git diff --check` passed.

Skipped tests: None.

The native checks use mock devices, loopback peers or fake driver APIs.
They do not certify physical hardware or add unsupported live operations.
See [architecture and protocol capability limits](architecture.md).

## Artifact SHA-256

The following artifacts are in `dist/` and `npm/`, excluded from Git.
These hashes identify the exact verified outputs; rebuilding can change them.

| Artifact | SHA-256 |
|---|---|
| `industriagents_profibus_mcp-0.3.0-py3-none-any.whl` | `9c51d094661c7e7ee2e0e66cf319bc4c1e70fba9f980bb0b55ee359b838d92a7` |
| `industriagents_profibus_mcp-0.3.0.tar.gz` | `c7cf877a2a73568e24a83b3cbe6861737ea6d53eaf7e8c2e243dd199ca4b329f` |
| `industriagents-profibus-mcp-0.3.0.tgz` | `e046ee7d929c16837bc9410c94554e5266a33e06f4eb823dc782b0538046c123` |

See [release instructions](release.md) for independent registry publication.
