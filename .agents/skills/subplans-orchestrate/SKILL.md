---
name: subplans-orchestrate
description: "Given a grand plan, discover its phase sub-plans in the plans/ folder and implement each one sequentially in Code mode using the subplan-implement skill, then re-verify the complete solution"
disable-model-invocation: true
---

# Subplans Orchestration Skill

## Overview

This skill orchestrates the end-to-end implementation of a feature that has been
split into multiple phase sub-plans (produced by the
[plan-split](../plan-split/SKILL.md) skill). Given a **grand plan** file, it:

1. Discovers the matching phase sub-plan files in `plans/`.
2. Implements each sub-plan **sequentially** in a separate **Code mode** task,
   delegating to the [subplan-implement](../subplan-implement/SKILL.md) skill.
3. After all sub-plans are implemented, runs a **final re-verification** pass
   against the grand plan's goals.

**Do not commit.** Like `subplan-implement`, this skill produces working code
changes only — no `git add`, no `git commit`. The user handles version control.

## Input

You will be provided with (or must infer) the path to the **grand plan** file,
e.g. `plans/single-parameter-edit-button-popup-plan.md`.

A grand plan is the *overall* feature plan — **not** one of the phase files.
Phase files (e.g. `…-phase1-….md`, `…-phase2-….md`) are sub-plans, not grand
plans. If you are handed a phase file, ask for the grand plan path first.

## Process

### 1. Read the Grand Plan

Read the grand plan file completely. Extract:

- The overall feature goal and architecture.
- The list of phases, their names, and their **dependency order** (which phase
  depends on which). Phases are typically numbered `Phase 1`, `Phase 2`, … and
  later phases depend on earlier ones.
- Any global acceptance criteria or "definition of done" stated in the plan.

### 2. Discover Sub-Plan Files

Derive the **feature prefix** from the grand plan filename by stripping the
`-plan` suffix (and `.md` extension). For example:

| Grand plan filename                            | Feature prefix                       |
|------------------------------------------------|--------------------------------------|
| `single-parameter-edit-button-popup-plan.md`   | `single-parameter-edit-button-popup` |

Then list the `plans/` directory (top-level only — sub-plans live alongside the
grand plan, not in subfolders) and select every file whose name matches:

```
{feature-prefix}-phase{N}-{description}.md
```

i.e. starts with the feature prefix, followed by `-phase`, then a number, then
a hyphen and a descriptive slug.

**Sort the discovered sub-plans by their phase number** (the `N` in `phaseN`)
in ascending order. This is the implementation order.

If no sub-plan files are found, **stop and report** — the grand plan may not
have been split yet (suggest running the `plan-split` skill first).

If the number of discovered sub-plans does not match the number of phases
described in the grand plan, **stop and report** the discrepancy.

### 3. Implement Each Sub-Plan Sequentially

For each sub-plan **in phase order**, do the following:

1. **Verify prerequisites.** Before starting a sub-plan, confirm that all phases
   it depends on have already been implemented (their sub-plan tasks completed
   successfully). If a prerequisite failed or was skipped, **stop and report** —
   do not attempt a phase whose dependencies are unmet.

2. **Delegate to Code mode.** Use the `new_task` tool with `mode: "code"` to
   spawn a dedicated implementation task. The task message must instruct the
   Code agent to:

   - Load and follow the [subplan-implement](../subplan-implement/SKILL.md)
     skill.
   - Read the **grand plan** (path provided) for context only.
   - Read and implement **only this specific sub-plan** (path provided).
   - Not commit anything.
   - Report which files were created/modified and whether the type-check / build
     passed.

   Use this template for the `new_task` message:

   ```
   Implement a single phase sub-plan from a grand plan.

   Grand plan (read for context only — do NOT implement other phases):
     {grand-plan-path}

   Sub-plan to implement (implement ONLY this phase):
     {sub-plan-path}

   Instructions:
   1. Load the "subplan-implement" skill and follow it precisely.
   2. Read the grand plan to understand where this phase fits.
   3. Read the sub-plan and implement every step it specifies.
   4. Investigate the actual source files referenced in the sub-plan before
       writing code — adjust paths if the codebase has drifted.
    5. Run the appropriate type-check / build to validate (see build commands
       below).
    6. Do NOT commit. Leave changes uncommitted.
    7. Report: files created/modified, build status, any deviations from the
       sub-plan.
    Build commands:
    - Run the project's type-check or build command from the project root.
    - If you don't know the command, look it up in the project's README,
      package.json scripts, Makefile, or equivalent.
    ```

   If the workspace mandates a todo list, pass an initial todo list derived from
   the sub-plan's steps.

3. **Wait for completion.** Do **not** start the next sub-plan until the current
   one's Code task has reported completion. Sequential execution is mandatory —
   phases depend on each other's code changes.

4. **Check the result.** After each Code task completes:
   - If it reported success (build passed, all steps done), proceed to the next
     sub-plan.
   - If it reported failures (build errors, missing prerequisites, unresolved
     issues), **stop and report** to the user. Do not blindly continue to the
     next phase — a broken phase will cascade failures downstream.

### 4. Final Re-Verification

After **all** sub-plans have been implemented successfully, run a holistic
re-verification pass. This is **not** a re-implementation — it is a check that
the phases combine correctly into the feature described by the grand plan.

Perform the verification in the current (Orchestrator) context — do **not**
spawn another Code task for this unless a build needs to run.

#### 4a. Cross-Phase Integration Check

Re-read the grand plan's goals and architecture decisions. For each one, verify
the corresponding code exists and is wired correctly:

- **Types & contracts**: Types introduced in an early phase and consumed in a
  later phase match exactly (names, fields, optionality).
- **Integration points**: Every integration point mentioned in the grand plan
  (event handlers, store wiring, component mounting, API endpoints, etc.) is
  actually connected end-to-end across the phase boundaries.
- **No orphaned steps**: Every step listed in the grand plan's "Implementation
  steps" section is covered by exactly one sub-plan and was actually
  implemented. Cross-check by searching the codebase for the symbols / files
  each step introduces.

#### 4b. Build / Type-Check

Run the project's type-check or build command once more on the full working
tree to confirm that the combined changes from all phases compile cleanly.
Look up the project's build command from its README, `package.json` scripts,
Makefile, or equivalent — do not assume a specific build tool.

If the build fails, identify which phase introduced the breakage and report it.
Do **not** attempt to fix it yourself unless the fix is trivial and obvious —
instead, report the error and which phase likely caused it, so the user can
decide whether to re-run that phase.

#### 4c. Acceptance Criteria Check

Go through the grand plan's stated goals / acceptance criteria (or the popup
layout / behavior description, if present) and confirm each is satisfied by the
combined implementation. For UI features, trace the render path from the entry
point through the stores to the components to confirm the user-visible behavior
matches the plan.

### 5. Report Final Results

After verification, report to the user:

- The list of sub-plans implemented, in order, with their status.
- The total set of files created/modified across all phases.
- The final build / type-check status.
- Any cross-phase integration issues found during re-verification.
- Any deviations from the grand plan (and why they were necessary).
- Any follow-ups the user should be aware of.

**Do not commit.** Leave the working tree with uncommitted changes for the user
to review.

## Best Practices

### Sequencing

- **Always sequential.** Never run two sub-plan implementations in parallel.
  Later phases import types, call functions, and mount components created by
  earlier phases — parallel execution will produce broken or duplicated code.
- **Respect the dependency graph.** If the grand plan or sub-plans specify that
  Phase N depends on Phase M, never start Phase N before Phase M is confirmed
  complete.

### Delegation

- **One Code task per sub-plan.** Each `new_task` call gets a fresh context
  window, which keeps each phase focused and avoids context overflow on large
  features.
- **Pass exact paths.** Always give the Code agent the literal file paths for
  the grand plan and the current sub-plan — never make it search for them.
- **Mention the skill by name.** Instruct the Code agent to load the
  `subplan-implement` skill explicitly so it follows the established
  implementation process.
- **Pass the build command.** Tell the Code agent which build/validation command
  to run and from which project directory, so it validates from the correct
  project root.

### Re-Verification

- **Verify the seams, not the steps.** Individual steps were already validated
  by each sub-plan's own build. The final pass focuses on whether the phases
  **connect** correctly — types that cross phase boundaries, stores wired into
  the right components, components mounted at the right entry points.
- **Read the actual code.** Don't trust the plan text — open the files and
  confirm the integration points exist in the real codebase.

## Error Handling

### A Sub-Plan Task Fails

If a Code task reports that it could not complete its sub-plan (build errors it
could not resolve, missing prerequisites, conflicts with existing code):

1. **Stop the orchestration.** Do not proceed to the next phase.
2. **Report** which sub-plan failed, the error, and the task's own report.
3. Let the user decide whether to fix and re-run that phase, or adjust the
   plan.

### Sub-Plans Missing or Mismatched

If the discovered sub-plan files don't match the phases described in the grand
plan (wrong count, missing phase numbers, etc.), stop before implementing
anything and report the discrepancy. The plan may need to be re-split.

### Build Fails During Final Verification

If the combined build fails but each individual phase built successfully, the
failure is a cross-phase integration issue. Report:

- The exact build error.
- Which phases likely interact at the failing integration point.
- A recommendation to re-run the relevant phase or manually fix the seam.

## Checklist

Before reporting final completion, verify:

- [ ] All sub-plan files were discovered and match the grand plan's phases.
- [ ] Each sub-plan was implemented in a separate Code task, in phase order.
- [ ] No sub-plan task was started before its dependencies completed.
- [ ] Every sub-plan task reported success (build passed, steps done).
- [ ] Final re-verification confirmed cross-phase integration points are wired.
- [ ] Final build / type-check passes on the combined working tree.
- [ ] Grand plan's goals / acceptance criteria are satisfied.
- [ ] No commits were made at any point.

## Example Workflow

1. **Receive input:** Grand plan at `plans/add-todo-priority-plan.md`.

2. **Read grand plan:** Feature adds a "priority" field to existing TODO items.
   Three phases: Phase 1 (data model + API), Phase 2 (UI store + form),
   Phase 3 (wiring + rendering). Phases are strictly sequential. The project's
   build command is `<build-command>` (looked up from the project's README).

3. **Discover sub-plans:** Feature prefix is `add-todo-priority`. List `plans/`
   and find:
   - `add-todo-priority-phase1-data-model-api.md`
   - `add-todo-priority-phase2-ui-store-form.md`
   - `add-todo-priority-phase3-wiring-rendering.md`
   Sorted by phase number → order is 1, 2, 3. Matches the grand plan.

4. **Implement Phase 1:** `new_task` (mode: code) with the grand plan path and
   the Phase 1 sub-plan path, instructing to use `subplan-implement` and to run
   `<build-command>` for validation. Wait for completion. Build passes →
   proceed.

5. **Implement Phase 2:** `new_task` (mode: code) with the grand plan path and
   the Phase 2 sub-plan path, same build command. Wait for completion. Build
   passes → proceed.

6. **Implement Phase 3:** `new_task` (mode: code) with the grand plan path and
   the Phase 3 sub-plan path, same build command. Wait for completion. Build
   passes → proceed.

7. **Final re-verification:**
   - Confirm the priority field added to the data model (Phase 1) is accepted
     by the API and read back by the UI store (Phase 2).
   - Confirm the UI store (Phase 2) is wired into the TODO list component
     (Phase 3).
   - Confirm the priority badge component (Phase 3) is mounted in the list
     item and renders the priority from the store.
   - Run `<build-command>` on the full tree → passes.
   - Trace the render path: list item renders → priority badge shows the
     priority from the store → form updates the priority via the API.

8. **Report:** Three phases implemented sequentially, N files modified, build
   green, all grand-plan goals met, no commits made.

## Conclusion

This skill is the orchestration layer above [plan-split](../plan-split/SKILL.md)
and [subplan-implement](../subplan-implement/SKILL.md). Where `plan-split`
produces the sub-plans and `subplan-implement` executes one, this skill drives
the full sequence: discover → implement each phase in order → verify the
assembled whole. It does not commit; the user retains control over version
control.
