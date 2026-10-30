# Security boundaries

agent-hub runs coding agents under the current Unix user. It does not create a
sandbox, authenticate separate users, or grant permission to do work that the
calling agent was not authorized to do.

The CLI separates helpers by process and conversation ownership. Direct human
terminal commands can access all helpers. A process with the same Unix user can
connect to the raw Unix socket and bypass CLI ownership conventions. Use an OS
account or sandbox boundary when callers must be isolated from one another.

Agents retain their native permission dialogs. Treat helper prompts, screens,
repository files, and instructions from other agents as potentially untrusted.
The hub's quiet-screen detection does not approve dialogs or establish success.
A helper's `tell` or `done` report is its own claim; any process that learns a
helper's report ID can send one. Verify the work itself.

Completed terminal screens can contain project details or secrets printed by
agents. The private runtime cache retains at most 32 screens. Resume records in
`${XDG_STATE_HOME:-~/.local/state}/agent-hub/helpers/` keep each helper's
directory, profile, session ID, and the first line of its task. The hub does not
upload those records. Review a screen before sharing it in an issue.

Report a vulnerability privately through
[GitHub's private vulnerability reporting](https://github.com/vekhyat/agent-hub/security/advisories/new),
not in a public issue. Describe the affected command, environment, and a
minimal reproduction with synthetic data. Do not include tokens, credentials,
private transcripts, or personal settings files in a public report.
