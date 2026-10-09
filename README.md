# Autonomous Stock Exchange Trading Desk

An autonomous, paper-trading-first trading system engineered with deterministic risk controls, committee deliberation, and operational recoverability.

> [!IMPORTANT]
> **Paper Trading Only**: This project operates strictly in simulation / paper-trading mode. Live order submission, real-money execution, and real brokerage credentials are strictly prohibited. All trading actions must preserve the $100 sandbox risk invariants.

---

## 1. System Invariants & Risk Constraints

The deterministic risk engine enforces the following invariants:
- **Sandbox Capital**: Exactly **$100.00 USD**.
- **Concurrent Positions**: Maximum **3 concurrent positions** ($28.00–$30.00 each).
- **Cash Buffer**: Permanent uninvested reserve of **$10.00 (10%)** to absorb slippage and margin variance.
- **Risk Cap Per Trade ($R$)**: Maximum risk per trade is strictly capped at **$3.00 (3.0% of $100)**.
- **Two-Tier Circuit Breaker**:
  - *Tier 1 Soft Freeze ($\text{Equity} \le \$80.00$)*: Halts new buy orders; existing positions remain managed by brackets.
  - *Tier 2 Hard Liquidation ($\text{Equity} \le \$70.00$)*: Liquidates open positions and locks trading via `HALTED.lock`.
- **Fail-Closed Policy**: Missing, stale, or malformed data immediately halts execution with an audit log.

---

## 2. Prerequisites

- **Python**: 3.12 (pinned in `pyproject.toml`)
- **Virtual Environment**: `.venv` using Python 3.12

---

## 3. Environment Setup

### Create and Activate Virtual Environment

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

### Install Dependencies

Dependencies are pinned in [pyproject.toml](file:///Users/Thunderops/Documents/Projects/stock-exchange/pyproject.toml) reflecting the verified project environment.

```bash
pip install --upgrade pip
pip install -e ".[dev]"
```

---

## 4. Local Development Workflow

All standard quality and testing tools can be run via the virtual environment or using `python3 -m <tool>`.

### Testing

Run the test suite using `pytest`:

```bash
# Fast test run (standard validation command)
./.venv/bin/pytest -q
# or with active virtual environment:
pytest -q

# Run with test coverage reporting
./.venv/bin/pytest --cov=src -q
# or:
python3 -m pytest --cov=src -q
```

### Formatting

Check or apply code formatting using `ruff`:

```bash
# Check formatting without modifying files
./.venv/bin/ruff format --check .
# or:
ruff format --check .

# Automatically apply formatting
./.venv/bin/ruff format .
# or:
ruff format .
```

### Linting

Check code quality and catch syntax / import errors:

```bash
# Run linter
./.venv/bin/ruff check .
# or:
ruff check .

# Automatically fix fixable lint issues
./.venv/bin/ruff check --fix .
# or:
ruff check --fix .
```

### Type Checking

Verify static typing with `mypy`:

```bash
# Run type checking on source code
./.venv/bin/mypy src
# or:
python3 -m mypy src
```

---

## 5. Running the Application

### Daily Trading Cycle CLI

```bash
./.venv/bin/python src/main.py --mode daily
```

### Observability Dashboard

```bash
./.venv/bin/streamlit run src/observability/dashboard.py
```

---

## 6. Continuous Integration

The repository includes a GitHub Actions workflow in [.github/workflows/ci.yml](file:///Users/Thunderops/Documents/Projects/stock-exchange/.github/workflows/ci.yml) that executes on all pushes and pull requests to `main` / `master`:
- Checks out code on `ubuntu-latest`.
- Sets up Python 3.12.
- Installs pinned dependencies (`pip install -e ".[dev]"`).
- Runs linting (`ruff check .`).
- Runs static type-checking (`mypy src`).
- Executes the test suite (`pytest -q`).

---

## 7. Security and Credentials

- **Zero Secret Storage**: Never commit API keys, tokens, or credentials into source files, test fixtures, or configuration files.
- **Environment Variables**: Configure all sandbox API keys (e.g. paper broker keys) using local environment variables or an untracked `.env` file (see `.gitignore`).
- **Paper Trading Only**: Ensure broker configuration targets paper / sandbox endpoints only.
