# Agent instructions (Codex)

## Project
This is a simple Python repo with a single entrypoint: `main.py`.

## Setup
Dependencies are installed via:
- `pip install -r requirements.txt`
- `pip install -r requirements-dev.txt`

## Quality bar (must do before finishing any task)
Run these commands and fix any failures:
1) Format:
   - `ruff format .`
2) Lint:
   - `ruff check .`
3) Tests:
   - `pytest`

## Rules
- Keep changes minimal and simple.
- Prefer small functions in `main.py` so they can be tested.
- If something fails, fix it rather than skipping checks.
