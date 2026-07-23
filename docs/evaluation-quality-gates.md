# Evaluation quality gates

Each suite owns an immutable closed set of eight canonical Decimal thresholds under `phase9-quality-gates-v1`. Import accepts only finite ASCII decimal strings from zero through one with at most four fractional digits; floats, defaults, missing keys, and unknown keys are rejected.

Gate comparison uses unrounded `Decimal` aggregates. A run has `gate_status=passed` only when every threshold passes. If metric computation and artifact publication succeed but a threshold fails, the run remains `succeeded` with `gate_status=failed` and an exact failed-gate count. Infrastructure or authority failures instead produce a failed run with no final result artifact or completion-success audit.

Invariant behavior still requires zero cross-department or unauthorized accepted candidates, no missing applicable metric, and invalid-contract rate at or below the suite maximum. A passing or failing gate never changes production retrieval, prompts, models, documents, feedback, evaluation thresholds, training data, or adapter state automatically.
