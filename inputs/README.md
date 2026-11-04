# Dataset Inputs

The current verifier requires Lean **4.35.0** and Mathlib commit
`56a005855ea17e0b0958f73ec801ae140b0a1679`.

| Input | Tasks | Version status |
| --- | ---: | --- |
| `fate/fate_h_lean435.jsonl` | 100 | Pinned to the supported workspace |
| `fate/fate_x_lean435.jsonl` | 100 | Pinned to the supported workspace |
| `putnam/putnam200.jsonl` | 200 | Pinned to the supported workspace |
| `deepseek_proverbench/deepseek_proverbench.jsonl` | 325 | Original imported tasks; no workspace version pinned |
| `matholympiadbench/matholympiadbench.jsonl` | 360 | Original tasks pinned to Lean 4.9.0-rc1; port before evaluating |
| `proofnet/proofnet.jsonl` | 371 | Original tasks pinned to Lean 4.9.0-rc1; port before evaluating |

ProofNet also has separate `proofnet_test.jsonl` (186 tasks) and
`proofnet_valid.jsonl` (185 tasks). These are subsets of the combined file.
The original imported datasets are retained as source material; inclusion here
does not establish compatibility with the current verifier. Changing version
metadata alone does not port a theorem.

Each JSONL row contains the task, its metadata, and workspace requirements.
Where present, `metadata.source_repo` and `metadata.source_revision` identify
the upstream source. Datasets retain their upstream licenses; the repository's
MIT license covers DVP code. See [usage](../docs/usage.md) for evaluation commands.
