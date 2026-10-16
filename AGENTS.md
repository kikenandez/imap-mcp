# Public repository rules

- Keep personal and client information out of tracked files, commit messages,
  pull-request descriptions, release notes, and build artifacts.
- Use only synthetic fixtures and reserved example.com, example.org, example.net
  or .test domains for email addresses and account-specific hosts in examples.
  Public provider documentation and generic provider endpoints are permitted.
- Never commit real account configuration, credentials, message samples,
  integration briefs, diagnostic output, or test-result output files.
- Preserve upstream copyright and license notices.
- Resolve passwords inside the server; never ask an assistant to retrieve or
  print a real password for testing. Tests must fake both IMAP and Keychain.
- Before publishing, review the staged diff and Git identity. Use the repository's
  GitHub username and GitHub noreply identity for new commits and release tags.
- Run locked pytest and Ruff checks. Publish release tags only on reviewed commits
  with passing CI; include the exact release commit SHA in the handoff.
