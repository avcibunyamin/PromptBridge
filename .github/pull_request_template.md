## Summary
<!-- Provide a clear and concise summary of the changes proposed in this pull request and their motivation. -->

## Tests Run
Please ensure and verify that the following test suites have been executed and pass:

- [ ] `cd ai-orchestrator && .venv/bin/python -m unittest discover -s tests`
- [ ] `cd ui && npm test && npm run build` (or from repository root: `cd ai-orchestrator/ui && npm test && npm run build`)

## Security Boundaries Checklist
Please verify that your changes adhere to PromptBridge's security invariants:

- [ ] **Loopback only:** Services listen strictly on loopback (`127.0.0.1`), never exposed to local network (`0.0.0.0`).
- [ ] **Mutation guard on mutating endpoints:** Every state-mutating HTTP endpoint (`POST`, `PUT`, `DELETE`) is protected with `mutation_guard`.
- [ ] **No shell for prompts:** Prompts, file paths, and subprocess arguments are never passed through a shell (`sh -c`); fixed argument arrays (`execve`) only.
- [ ] **Reviewers read-only:** Reviewer profiles and tools remain strictly read-only (`READ_ONLY`).

## Privacy & Personal Data
- [ ] **No personal data:** Verified that no usernames, email addresses, personal filesystem paths (e.g., `/Users/<name>/`), secrets, or tokens are included in code, tests, or documentation.
