# Contributing to Yada

Thanks for helping improve Yada. This guide covers local setup, validation, and
the Git workflow expected for pull requests.

Before changing behavior, read the [architecture](docs/dev/architecture.md).
For failures and trace inspection, use the
[debugging guide](docs/dev/debugging.md). User-facing commands belong in the
[CLI reference](docs/cli-reference.md).

## Development setup

Fork `GenTang/Yada` on GitHub, then clone your fork and add the canonical
repository as `upstream`:

```bash
git clone https://github.com/YOUR-USERNAME/Yada.git
cd Yada
git remote add upstream https://github.com/GenTang/Yada.git
git remote -v

uv sync --locked --dev
```

Create a feature branch from the latest `upstream/main`:

```bash
git fetch upstream
git switch -c feature/short-description upstream/main
```

Keep unrelated changes in separate branches and pull requests. Do not commit
generated workspaces, evaluation results, traces, virtual environments, caches,
API keys, or other secrets.

## Make and validate a change

Run focused tests while developing, then run the full local CI suite before
opening or updating a pull request:

```bash
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest tests/ -v
```

To apply Ruff formatting locally:

```bash
uv run --frozen ruff format .
```

CI runs lint, formatting, and tests on Python 3.11 and 3.12. If a behavior change
cannot be covered by a deterministic offline test, explain why in the pull
request and provide the smallest reproducible trace or benchmark case available.

Documentation changes should keep `README.md` and `README-cn.md` structurally
aligned. Never put a real API key or an unreviewed debug trace in an issue or PR.

## Keep your branch current with rebase

Yada uses a rebase workflow. Update your feature branch from `upstream/main`
without creating merge commits:

```bash
git fetch upstream
git rebase upstream/main
```

Do not use `git merge upstream/main` on a feature branch. A linear history keeps
review and later bisection focused on the actual change.

### Resolve rebase conflicts

When Git stops on a conflict:

```bash
git status
# Edit each conflicted file and remove conflict markers.
git add path/to/resolved-file
git rebase --continue
```

Repeat until the rebase finishes. To abandon the entire attempt and restore the
branch to its pre-rebase state:

```bash
git rebase --abort
```

Use `git rebase --skip` only when the stopped commit is genuinely redundant; it
drops that commit from the rewritten branch.

## Clean up commits before the PR

Use interactive rebase to reorder, reword, fix up, or squash noisy development
commits into a small set of logical changes:

```bash
git fetch upstream
git rebase -i upstream/main
```

Do not squash unrelated behavior, tests, and documentation merely to reach one
commit. The goal is reviewable history, not a specific commit count.

Then run the full validation suite again and push the branch:

```bash
git push -u origin feature/short-description
```

Open a pull request against `GenTang/Yada:main` and complete the repository's PR
template with:

- what changed and why;
- the exact validation commands and results;
- benchmark, token, latency, or trace impact, or `N/A` when not applicable.

## Update a PR after review

Make the requested changes, commit them, and rebase again before updating the
remote branch:

```bash
git fetch upstream
git rebase upstream/main
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest tests/ -v
git push --force-with-lease origin feature/short-description
```

Rebase rewrites commit IDs, so a normal push will be rejected. Use
`--force-with-lease`, never plain `--force`: the lease refuses to overwrite
remote work you have not seen.

If another contributor also writes to your branch, coordinate before rebasing
or force-pushing it.

## Review scope

A pull request is ready for review when it is focused, tested, documented, and
rebased onto the current `upstream/main`. Maintainers may ask for additional
benchmark evidence when a change affects prompts, tools, context, verification,
or model-call behavior.
