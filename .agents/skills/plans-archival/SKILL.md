---
name: plans-archival
description: "How to archive completed plan files — extract reusable knowledge into project memory, then move top-level plans to plans/archive and remove subplan folders after analysis"
disable-model-invocation: true
---

# Plans Archival Skill

## Overview

This skill processes completed implementation plans: reads all plan files in the `plans/` folder (recursively, including any subfolders), extracts knowledge worth preserving for future implementations into the project memory system, then moves top-level plan files (grand-plans and standalone plans) to `plans/archive/` and removes the subplan folders after their knowledge has been extracted.

Plan files in `plans/` fall into two categories:
- **Top-level plans** — `.md` files directly in `plans/` (grand-plans and standalone plans). These get archived.
- **Subplans** — files inside subfolders like `plans/feature-a/`, `plans/feature-b/`. These are **read** for context (they contain phase details and resolved ambiguities), their knowledge is extracted into memory, and the subfolders are then **removed** — they are not moved to archive.

The goal is to capture **reusable architectural knowledge** — not implementation details that are already visible in the code itself.

## Process

### 1. Load Project Memory

Follow the standard preamble:

1. Read [`memory/MEMORY.md`](../../memory/MEMORY.md) — the central index of the knowledge base.
2. Read all knowledge files listed in `MEMORY.md` — you need to know what is **already documented** to avoid duplication and to identify gaps.

### 2. Read All Plan Files

1. List all files in `plans/` recursively — both top-level plans and files inside subplan subfolders.
2. Read every plan file and every asset file. Plans often reference each other (phases, dependencies, shared assets like requirements docs and Q&A logs) — read them all before analyzing. Subplan folders contain the richest knowledge (resolved ambiguities, field mappings) — read them even though they will not be archived.

### 3. Read Related Implementations (if needed)

If a plan references source files and you are unsure whether the plan was actually implemented (or how it was implemented), read the referenced source files to verify the current state. This helps distinguish:
- **Implemented knowledge** — patterns and mappings that now live in the codebase and should be documented in memory.
- **Planned but not yet implemented** — note these but do not write them to memory as established patterns.

### 4. Analyze and Decide What to Save

For each plan, evaluate its content against these criteria:

#### SAVE to memory — reusable knowledge that is NOT obvious from reading the code:

| Knowledge type | Example | Why it matters |
|----------------|---------|----------------|
| **Entity-to-API field mappings** | A form field maps to an API field, but the mapping is spread across a store, an API schema, and a validation spec — not visible in any single file | Cross-layer mappings are hard to reconstruct from code alone |
| **Cross-system data flow** | "The count is fetched asynchronously after a continuation token is acquired" | Explains why the code is structured a certain way; not obvious from reading the store alone |
| **Resolved ambiguities** | "Two input fields map to one API field; only one may be filled at a time" | The resolution of a design question that took analysis — future developers would otherwise re-derive it |
| **Architectural patterns** | "A service must wrap calls to an external dependency with the framework's reactivity context to stay consistent" | A reusable pattern applicable beyond the specific feature |
| **Hardcoded values and why** | `status = 1` means "active for new entries" | The "why" is not in the code |
| **What is NOT sent / computed / derived** | A rate field is computed as `total / count` locally and not sent to the API | Prevents future confusion about "missing" fields |

#### DO NOT SAVE to memory — implementation details or already-documented knowledge:

| Knowledge type | Why not |
|----------------|---------|
| **Step-by-step implementation instructions** | Execution detail; the code is the source of truth after implementation |
| **Exact code snippets from plans** | The code itself is authoritative; memory should describe patterns, not duplicate code |
| **Phase ordering and dependencies** | Only relevant during implementation; not reusable afterward |
| **Already documented in a knowledge file** | Check existing memory first — do not duplicate |
| **Covered by an existing skill** | e.g. endpoint creation conventions are covered by a relevant skill in the project |
| **Compiler-enforced exhaustiveness** | e.g. exhaustive pattern matching — the compiler catches missing cases | The compiler catches missing keys/cases; documenting is redundant |
| **Transient one-off changes** | e.g. adding a few enum variants to a validation schema — no pattern to reuse | |

### 5. Write Memory

Based on the analysis, create or update memory files:

#### Creating a new knowledge file

When a plan documents a substantial subsystem (e.g. a drawer with 29 fields, a complex store with cross-validation and computed fields), create a dedicated knowledge file:

- **File name**: descriptive, context-rich (e.g. `create-bp-drawer.md`, not `form.md` or `notes.md`).
- **Location**: `memory/` for project-specific knowledge, or a subdirectory under `memory/` if the project organizes knowledge by component or subsystem.
- **Structure**:
  - `# Title` + `Purpose:` one-liner
  - `## Location` — file paths with line links
  - Domain-specific sections (mappings, patterns, flows)
  - Cross-references to other knowledge files

#### Updating an existing knowledge file

When a plan reveals a pattern or gotcha that belongs in an existing file (e.g. a coding convention, a reactive pattern), append a new `## Section` to that file. Do not rewrite existing content.

#### Memory rules (from project MEMORY.md)

- **DO NOT** add implementation details to memory
- **ONLY** add relations between system parts and entities
- Focus on data flow, dependencies, and architectural patterns

#### Registering new files

After creating a new knowledge file, add it to the `MEMORY.md` index table:

```markdown
| [`filename.md`](filename.md) | When to read this file |
```

If the knowledge file lives in a different memory directory (e.g. a shared library's memory), update **that** memory's `MEMORY.md` index instead.

### 6. Move Top-Level Plans to Archive and Remove Subplan Folders

After all memory updates are complete:

1. Move **only top-level** plan files (grand-plans and standalone plans) to `plans/archive/`:

```sh
mkdir -p plans/archive
mv plans/*.md plans/archive/
```

2. Remove the subplan folders — their knowledge has already been extracted into memory in the previous steps, so the folders are no longer needed:

```sh
rm -rf plans/feature-a plans/feature-b plans/feature-c
```

Adjust the folder list to match the actual subplan subfolders present in `plans/`.

Requirements:
- **Only** `.md` files directly in `plans/` (top-level) move to `plans/archive/` — these are grand-plans and standalone plans.
- **Remove subplan folders** (e.g. `plans/feature-a/`, `plans/feature-b/`, `plans/feature-c/`) after their knowledge has been extracted into memory. Do not move them to archive — delete them.
- After moving and removing, `plans/` contains only the `archive/` subfolder.
- Verify with `ls plans/archive` that the top-level plan files are present, and with `ls plans` that no subplan subfolders remain.

### 7. Report

Summarize what was done:
- Which memory files were created or updated and why.
- What knowledge was deliberately NOT saved and why.
- Confirm top-level plan files were moved to `plans/archive/`.
- Confirm subplan folders were removed after knowledge extraction.

## Checklist

- [ ] Project memory loaded (index + all knowledge files)
- [ ] All plan files read recursively
- [ ] Related source files read (if needed to verify implementation state)
- [ ] Each plan evaluated against SAVE / DO NOT SAVE criteria
- [ ] New knowledge files created (with descriptive names)
- [ ] Existing knowledge files updated (appended, not rewritten)
- [ ] All project `MEMORY.md` indexes updated with new file entries
- [ ] Stale references to plan files in existing memory removed or updated
- [ ] Top-level plan files moved to `plans/archive/` (grand-plans and standalone plans only)
- [ ] Subplan folders removed after knowledge extraction (not moved to archive)
- [ ] `plans/` contains only `archive/` (no subplan subfolders remain)
- [ ] Summary report provided

## Common Pitfalls

1. **Saving implementation details** — Memory should capture *why* and *how systems relate*, not *what code to write*. If a section reads like a tutorial, it belongs in a plan, not memory.
2. **Duplicating existing memory** — Always read existing knowledge files first. If a pattern is already documented, do not re-document it.
3. **Missing subfolder contents during read** — Plans may have subfolders with subplans and Q&A logs. These contain the richest knowledge (resolved ambiguities, field mappings). Read them for context **before** removing the subplan folders.
4. **Removing subplan folders before extracting knowledge** — Subplan folders must be fully read and their knowledge extracted into memory **before** removal. Removing them too early loses the richest source of resolved ambiguities and field mappings.
5. **Leaving stale plan references** — After archiving and removing subplan folders, existing memory files may contain links like `[...](../../plans/some-plan.md)` or `[...](../../plans/feature-a/some-subplan.md)`. Remove or update these — the top-level plans are now in `plans/archive/` and the subplan folders no longer exist.
6. **Not updating the index** — A new knowledge file that is not listed in `MEMORY.md` is invisible to future agents.
7. **Overwriting instead of appending** — When updating an existing knowledge file, add new sections; do not rewrite or remove existing content.
