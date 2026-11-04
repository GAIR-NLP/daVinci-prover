# Tests

Tests are grouped by responsibility:

| Directory | Coverage |
| --- | --- |
| `agent/` | Agent loop, feedback, and conversation traces |
| `llm_clients/` | Model calls, authentication, routing, and cache probing |
| `toolkits/` | File operations and source search |
| `retrieval/` | LeanExplore local retrieval, remote clients, and server |
| `verification/` | Lean, parsing, fixed contracts, Comparator, context activation, and reverification |
| `orchestration/` | Batch scheduling, infrastructure recovery, and runtime provenance |
| `runtime/` | Runtime paths, package interfaces, and configuration profiles |
| `fixtures/` | Shared test data |

Run commands from the repository root:

```bash
python -m pytest -q
python -m pytest tests/retrieval -q
python -m pytest --collect-only -q
```

Some tests require Lean, Mathlib, Comparator, or optional Python dependencies. Missing dependencies may cause skips or failures; check the test output when running without these resources.
