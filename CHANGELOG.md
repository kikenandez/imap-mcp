# Changelog

All notable changes are documented here, following Keep a Changelog.

## [0.3.1] - 2026-10-16

### Added

- Inline HTML filtering for matching foreground/background colors, transparent
  colors, collapsed clipping and common off-screen text styles.
- Offline canary tests across both credential sources, all MCP tools, diagnostic
  modes and credential/backend failure paths.
- A Git-index hygiene check in CI for non-example email addresses, app-password
  patterns and forbidden local configuration, mail and integration files.
- Documentation of the fixed PKMA Keychain convention and the local-user and
  untrusted-mail threat models.

### Fixed

- Authentication errors no longer propagate server responses that could echo a
  password; IMAP debug output is disabled and its login command history cleared.
- Credential failures consistently identify the reference without backend output.

## [0.3.0] - 2026-10-16

### Added

- Connection-time environment and macOS Keychain password references.
- Bounded mail body reads, inline hidden-HTML filtering and scoped message IDs.

[0.3.1]: https://github.com/kikenandez/imap-mcp/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/kikenandez/imap-mcp/releases/tag/v0.3.0
