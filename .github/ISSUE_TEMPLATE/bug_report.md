---
name: Bug report
about: Report a problem with the PROFIBUS MCP server or the mock slave
title: "[Bug] "
labels: bug
assignees: ""
---

**Which part?**
- [ ] MCP server (`profibus-python`)
- [ ] Mock slave (`profibus-mock-slave`)

**Describe the bug**
What happens, and what you expected instead.

**To reproduce**
The MCP client you used (Claude Desktop / Claude Code / Cursor / Inspector /
IndustriConnect's mcp-manager-ui), the tool you called, and the arguments you
passed.

1.
2.
3.

**Tool output**
```json
paste the { success, data, error, meta } envelope, or the error
```

**What was on the other end?**
- [ ] The mock slave from this repo (`PROFIBUS_BACKEND=mock`)
- [ ] The `serial` backend (not implemented yet)
- [ ] Real equipment through something else — adapter or gateway, and slave vendor and model:

**Environment**
- OS:
- Python version:
- Commit SHA:
- Relevant settings (`PROFIBUS_BACKEND`, `PROFIBUS_WRITES_ENABLED`, `PROFIBUS_MOCK_PORT`, `PROFIBUS_PORT`, ...):

**Additional context**
Slave addresses, slave map, GSD file, stderr logs — whatever would let someone
else reproduce it against the mock. Do not paste credentials or production
process data.
