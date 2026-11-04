# DVP Documentation

[Back to the project README](../README.md)

| Task | Guide |
| --- | --- |
| Set up a new machine | [Installation and migration](installation.md) |
| Configure a model API, prove a theorem, or evaluate a dataset | [Quick start](usage.md#quick-start) |
| Save machine-specific paths and credentials | [Local configuration](local-configuration.md) |
| Troubleshoot evaluation | [Usage and troubleshooting](usage.md) |
| Configure retrieval, download models, or rebuild data and indexes | [Local LeanExplore retrieval](leanexplore.md) |
| Host retrieval on a separate machine | [Retrieval server](leanexplore-server.md) |
| Install Comparator and choose a verification mode | [Lean checks and Comparator](verification.md) |
| Run isolated local Python experiments | [Python experiments](python-experiments.md) |
| Browse the available datasets | [Dataset inputs](../inputs/) |
| Upgrade fixed contexts or resume runs | [Resume rules](architecture.md#resuming-runs-and-upgrading-fixed-contexts) |
| Find code and understand state, artifacts, and compatibility | [Architecture](architecture.md) |
| Inspect tools, saved results, and scoring | [Tools and run artifacts](architecture.md#tools-and-run-artifacts) |
| Rebuild retrieval data or diagnose model services | [Maintenance tools](../tools/README.md) |
| Review source-release checks and a real accepted proof | [Release validation](release-validation.md) |

Run commands from the repository root unless stated otherwise. `scripts/` contains setup, retrieval checks, and server startup; `tools/` contains optional data-build and diagnostic utilities. `examples/` contains portable configuration templates and `inputs/` contains tasks. Machine configuration belongs in ignored `.local/`; downloaded assets, runtimes, results, and reports stay in ignored `data/`, `runtime/`, `runs/`, `analysis/`, and `logs/` directories. These local files are not included in a source checkout.
