# 0.3.0 local release verification

Verified on 2026-10-15. Package: `industriagents-profibus-mcp`.

This records local build verification, not registry publication. Check the
GitHub Actions runs and registry version pages for publication status. Registry
authentication/trusted publishers must be configured before release; GitHub
repository access alone does not authorize registry publication.

## Checks

- Full Python regression run: 216 passed.
- Final architecture/contract tests: 15 passed (a separate run, not additional unique tests).
- Ruff lint and formatting checks passed for source and tests.
- `uv lock --check` and contract/package version parity passed.
- Three npm launcher tests passed.
- Installed wheel and installed npm tarball both initialized over stdio outside
  the checkout, reported version 0.3.0 and listed 7 canonical tools.
  Their schemas matched the contract; invalid mode calls returned MCP errors.
- `twine check --strict` passed for the wheel and source distribution.
- The npm tarball contains exactly the same wheel bytes as the PyPI artifact.
- `git diff --check` passed.

No tests were skipped in the full regression run.

These are software and mock/loopback checks, not physical-device certification.
See [protocol capability limits](architecture.md#protocol-capability-limits).

## Artifact SHA-256

Artifacts are local build outputs in `dist/` and `npm/`, excluded from Git.
Rebuilding can change archive hashes; these identify the verified files.

| Artifact | SHA-256 |
|---|---|
| `industriagents_profibus_mcp-0.3.0-py3-none-any.whl` | `b5465705c9914a877c90888d314cf82d1c3c82c93b3be62ca740aee1088336bc` |
| `industriagents_profibus_mcp-0.3.0.tar.gz` | `6b5a028043fe4494e58b7292a2510923aeebf6f3e87fdaa3794dc9ac92951a80` |
| `industriagents-profibus-mcp-0.3.0.tgz` | `8dcffe0a67c0482af564464e6fdbc486653c277b7aaf6f53e4bce59b21ee8aa7` |

See [release instructions](release.md) for independent registry publication.
