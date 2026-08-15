# gh-runners development tasks

# Run all checks (lint + typecheck + tests with coverage gate)
check: lint typecheck test

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

# Run the test suite with the coverage gate (fail_under = 95)
test:
    uv run pytest --cov --cov-report=term-missing

# Run the tests without the coverage gate — faster inner loop
test-fast:
    uv run pytest -q --no-cov

# Coverage as a browsable HTML report
coverage:
    uv run pytest --cov --cov-report=html
    @echo "open htmlcov/index.html"

# Run the CLI
run *args:
    uv run python -m gh_runners.cli {{ args }}
