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
| `industriagents_profibus_mcp-0.3.0-py3-none-any.whl` | `e774334aa8f666397e1c2202ec486878ff725109990994f89cb86a1ff1fc2430` |
| `industriagents_profibus_mcp-0.3.0.tar.gz` | `b569ab6cefbaa9ab92fda7898418bd5412303fd6ce7566c8ed3be33acf75df78` |
| `industriagents-profibus-mcp-0.3.0.tgz` | `9438d26e1081ddcc645e90a38f3039539e1dd8ee7fc4167c82b72f470e871be4` |

See [release instructions](release.md) for independent registry publication.
