# Contributing

## Setup

```bash
./scripts/setup.sh                   # deps, config, Google login
./scripts/doctor.sh                  # verify the environment
uv run --extra dev pytest tests packs   # the test suite: the core's and the packs'
uv run --no-group packs --extra dev pytest tests   # the core alone, with no pack installed
npm --prefix frontend run build      # the console (scripts/serve.sh rebuilds it when stale)
npm --prefix frontend test           # route parsing (node --test)
uv run --no-project --with playwright python frontend/tests/ui_smoke.py   # click through navigation on a running server
```

## Ground rules

These rules come from the design in the README. The tests named here enforce them.

- **The engine is generic.**
  - Nothing under `src/meeseek/` names a project, a company or a person.
  - Team data goes in `project.yaml`, `.env` or the knowledge base.
  - Prompts take `{{placeholders}}` (`context/prompts.py`).
  - Defaults are only in `config/schema.py` (`test_config.py`).
- **Prompt text has one home.**
  - Text that a model receives as an instruction is a file in `src/meeseek/prompts/`.
    The code reads it with `prompts.text("dir/name#section")`, and the README of that folder lists it.
  - The SOP of a skill is its `SKILL.md`.
  - A rule about one tool is in the description of that tool.
  - A tool result states facts. A sentence that tells the model what to do next is a prompt (`test_prompts.py`).
- **Code over prompt.** Some steps have one correct answer for their input: a query to build, a format to apply, a sequence of reads.
  Write such a step as code or as a tool. Do not write it as an instruction for the model.
- **Writes are granted.**
  - A tool that changes anything outside the process is `writes=True` in the registry.
  - A skill can use such a tool only when its `tools:` list grants it (`test_security_policy.py`).
  - Reads go to all agents.
- **Vendors are packs.**
  - The API of a vendor (an issue tracker, a git host, a cloud) is a pack under `packs/`.
  - A pack has its own package and its own MCP server. It uses `meeseek_pack_kit`, and its tests are next to it (packs/README.md).
  - The core never imports a pack, and a pack never imports meeseek (`test_boundaries.py`).
  - The tests of the core pass with no pack installed.
- **Docs follow the code.** The tests compare these items with the code (`test_docs_sync.py`):
  - README trees.
  - The skills table.
  - The MCP tool list.
  - `.env.example` and `project.yaml.example`.
  - Each code path that a doc or a docstring names.
- **Measure behaviour changes.** A change to a prompt, a tool description or the turn loop needs the harness eval before and after (`evals/README.md`).
  Write in the commit message what moved.
- **No secrets in commits.**
  - `project.yaml`, `.env`, `knowledge_base/notes/` and `skills/*/learned.md` are gitignored.
  - `client_secret.json` is also gitignored. It is an optional OAuth client for the Google sign-in.
  - Only `project.yaml.example` is committed, and each value in it is a placeholder.

## Pull requests

- Put one concern in each PR. Branch it from `main`.
- Write what changed and why.
- For behaviour changes, include test output or eval output.
- Keep `uv run --extra dev pytest tests packs` green.

## Language of docs and MR titles

- Documentation in this repo is in ASD-STE100 Simplified Technical English.
- Write MR titles in English, in the imperative, on one line.
- Put the ticket key first when there is one.

## Code style

Match the code around your change. The lint config is in `pyproject.toml` (`ruff`).
Comments can be in English or in Traditional Chinese. Use the language of the nearby code.
Prompts are in Traditional Chinese, because the agents answer in it.

### Comments and docstrings

Describe the code as it is now. Keep the information that a reader cannot get from the code:

- What a module or a function is for.
- Invariants that are not obvious.
- Constraints that external systems set (Gemini, GitLab, Drive).

Do not write these items, because they belong in the commit message:

- History ("used to", "replaces", what an earlier version did).
- Incident stories.
- Comments that restate the code.

When a past incident is the reason for a rule, state the rule in the present tense.
A test docstring says which behaviour the test pins.
