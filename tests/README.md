# tests/

Python unit and property-based tests for `agents/`, `tools/`, and `shared/`.

- Property-based tests use [Hypothesis](https://hypothesis.readthedocs.io/) and
  are annotated with `**Validates: Requirements X.Y**` docstrings linking back
  to the requirements document.
- Run with `pytest` from the repository root (see root `pyproject.toml`).
