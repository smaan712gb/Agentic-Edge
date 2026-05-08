# Contributing to Agentic Edge

Thanks for your interest. This is an early-stage project and the most useful contributions right now are bug reports, provider-specific issues, and pull requests against items on the roadmap.

## Ground rules

- **Paper trading only.** Code that targets a live brokerage account will not be merged. The whole point of the project is decision support; live execution is intentionally out of scope.
- **No secrets in PRs.** Use `.env.example` for new environment variables. The `.gitignore` excludes `.env`, the SQLite database, and build artifacts; please don't add them back.
- **Stay legible.** Prefer clear names and short functions over clever abstractions. The system is meant to be read by analysts, not just engineers.

## Local setup

1. Fork the repo and clone your fork.
2. Copy `.env.example` to `.env` and fill in whichever provider keys you have.
3. Follow the Quick Start in the README to get the API and the web app running.
4. The agent graph and provider SDK live under an `agents/` package the API imports. See `docs/ARCHITECTURE.md` for how it's organised.

## What we're looking for

- **Provider robustness.** Edge cases around after-hours quotes, rate limits, sparse option chains, and partial fills.
- **New themes.** If you've curated a thematic basket, contribute the JSON seed.
- **Backtesting hooks.** The autotrade loop is forward-only today; harnessing it to a historical replay would be welcome.
- **Documentation.** Walkthroughs of how a single run unfolds, screen recordings, agent-by-agent explainers.

## What we're not looking for

- Live-trading shims.
- Performance claims, benchmark scoreboards, or backtests presented as evidence of expected returns.
- Additions that surface vendor names in the user-facing UI. The frontend is intentionally vendor-agnostic.

## Pull requests

- One change per PR. Keep them small.
- Include a brief description of what the change does and why.
- Make sure `pytest tests/` passes for any code you touched.
- For TypeScript changes, run `npm run lint` in `web/`.

## Reporting security issues

Please do not open public issues for anything that looks like a security problem. See `SECURITY.md` for how to report it privately.
