@AGENTS.md

# Compact instructions

When compacting, keep: the current task, files changed, test results (failures only), open decisions, and any hard-rule concerns. Drop: file contents already written to disk, full logs, exploration dead ends.

# Token discipline

- Read only the files a task names; never scan `docs/`, `reports/` or `data/` unless asked.
- Review routines read `memory/packs/` only, following `prompts/*.md` exactly.
