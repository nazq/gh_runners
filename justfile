# gh-runners development tasks

# Run all checks (lint + typecheck)
check: lint typecheck

# Lint with ruff
lint:
    uv run ruff check .
    uv run ruff format --check .

# Fix lint issues and format
fix:
    uv run ruff check --fix .
    uv run ruff format .

# Strict type checking
typecheck:
    uv run mypy --strict gh_runners/

# Run the CLI
run *args:
    uv run python -m gh_runners.cli {{ args }}
