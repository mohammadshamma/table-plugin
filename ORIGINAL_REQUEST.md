# Original User Request

## Initial Request — 2026-07-02T20:53:46-07:00

Convert the SQLite Table Extension from a Gemini CLI extension into a workspace-local Antigravity plugin in-place.

Working directory: /Users/mshamma/src/table-extension
Integrity mode: development

## Requirements

### R1. Antigravity Plugin Configuration
Create the `plugin.json` manifest and `mcp_config.json` configuration at the workspace root to define the plugin metadata and its table MCP server.

### R2. Antigravity Skill Configuration
Create the `skills/table/SKILL.md` file by converting the contents of `TABLE.md` and adding appropriate YAML frontmatter.

### R3. Build and Test Verification
Ensure the extension is built successfully and the test suite passes.

## Acceptance Criteria

### Plugin Integrity
- [ ] `plugin.json` exists at the root, containing name, version, and description.
- [ ] `mcp_config.json` exists at the root, declaring the `table` MCP server pointing to `${extensionPath}/dist/server.js` with `cwd` set to `${extensionPath}`.
- [ ] `skills/table/SKILL.md` exists with correct YAML frontmatter and the documentation contents.
- [ ] The build command runs successfully and generates `dist/server.js` and copies `table_tool.py`.
- [ ] The test suite runs and all tests pass.
