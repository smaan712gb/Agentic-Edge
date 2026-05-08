# Security

## Reporting a vulnerability

Please **do not** open public issues for anything you suspect is a security
problem. Email the maintainers directly with a description and (if possible)
a minimal reproduction. We'll acknowledge receipt within a few days and
follow up with a remediation plan.

## Scope

Examples of what we treat as in-scope:

- Code paths that could leak provider API keys to logs, responses, or stack
  traces.
- Auth bypasses around the admin endpoints (kill switch, scheduler control,
  reconcile).
- Any path that could trigger live-broker order submission against a
  non-paper account, given that paper-only is enforced by design.
- Database queries vulnerable to injection.
- Cross-site scripting or CSRF in the web app.

Out of scope: vulnerabilities in upstream dependencies that are already
fixed in a newer release; reports against deployments not maintained by
this project; theoretical issues with no exploit path.

## Hardening notes for operators

If you're running this for yourself, a few things are worth doing:

- Set a strong `ADMIN_API_TOKEN`. The service rejects placeholder values,
  but it doesn't enforce a length minimum. A 32-byte URL-safe token is a
  reasonable default — the README shows the one-liner.
- Keep the API on `127.0.0.1` unless you've put a reverse proxy with auth
  in front of it.
- Never commit `.env` or the SQLite database. The shipped `.gitignore`
  excludes both.
- If you've ever pasted an API key into a chat, an issue, or a PR, rotate
  it.
