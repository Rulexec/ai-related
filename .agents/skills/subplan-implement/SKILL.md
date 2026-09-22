---
name: subplan-implement
description: How to implement a single phase sub-plan from a grand plan, following the detailed implementation guide without committing
---

# Subplan Implementation Skill

## Overview

This skill implements a single phase sub-plan produced by the [plan-split](../plan-split/SKILL.md) skill. It takes a grand plan (for context) and one sub-plan (for implementation details), then implements only that sub-plan.

**Do not commit.** This skill produces working code changes only — no `git add`, no `git commit`. The user will handle version control themselves.

## Input Requirements

You will be provided with:
1. **Grand Plan**: The overall plan containing multiple phases (read for context only — do not implement other phases)
2. **Sub-Plan**: The specific phase to implement (detailed implementation guide with file paths, code snippets, types, and tests)

## Process

### 1. Understand the Context

**Read the Grand Plan:**
- Understand the overall architecture and goals
- Identify where this phase fits in the dependency graph
- Confirm that prerequisite phases have already been completed (if not, stop and report)

**Read the Sub-Plan:**
- Understand what this specific phase accomplishes
- Review all files to be modified or created
- Note the implementation steps and code snippets
- Identify tests that need to be written

### 2. Investigate Existing Code

Before writing any code, read the actual source files referenced in the sub-plan:
- Understand the existing code structure and patterns around each insertion point
- Identify integration points with existing functionality
- Note naming conventions and coding styles used in the codebase
- Verify that file paths in the sub-plan match the real codebase (adjust if the codebase has changed since the plan was written)

**Key Files to Examine (adapt to the project's actual stack):**
- Backend: services, data access layers, API handlers, configuration
- Frontend: API clients, components, types/schemas, routes
- Gateway/routing layer: route definitions, middleware, schemas
- Shared: type definitions, shared utilities

### 3. Implement the Phase

**Follow the Sub-Plan Exactly:**
- Implement changes in the order specified by the sub-plan
- Use the exact file paths, function names, and type definitions from the plan
- Include all error handling as specified
- Write all tests as specified in the sub-plan

**Implementation Order:**
1. Create/modify data structures and types
2. Implement core logic functions
3. Add integration points (event handlers, API endpoints, service contracts, etc.)
4. Write unit tests
5. Write integration tests if specified

**Code Quality:**
- Follow existing codebase patterns and conventions
- Use the same naming conventions as the rest of the codebase
- Include all necessary imports
- Ensure code compiles without warnings

### 4. Validate Implementation

Run the project's type-check or build command. **Do not run tests unless the user explicitly asks.**

This catches type errors, missing imports, and signature mismatches.

**Manual Verification:**
- Review the implementation against the sub-plan
- Ensure all requirements from the phase are met
- Check that integration points are correctly wired

### 5. Report Results

After implementation and validation, report:
- Which files were created or modified
- Whether the type-check / build passed
- Any deviations from the sub-plan (and why they were necessary)
- Any issues or follow-ups the user should be aware of

**Do not commit.** Leave the working tree with uncommitted changes for the user to review.

## Best Practices

### Implementation

**Be Precise:**
- Follow the sub-plan exactly as written
- Don't add extra features not specified in the phase
- Don't refactor existing code unless the plan specifies it

**Handle Edge Cases:**
- Implement all error handling specified in the plan
- Handle edge cases mentioned in implementation notes
- Follow the validation logic provided

**Scope Discipline:**
- Implement only the current sub-plan — do not start work on other phases
- If you discover that a prerequisite phase is incomplete, stop and report
- If you discover that the sub-plan references something not yet implemented, stop and report

### Code Quality

**Follow Conventions:**
- Use existing patterns from the codebase
- Match naming conventions used by the codebase (e.g. camelCase, PascalCase, snake_case — whichever applies)
- Follow the project's code style

**Keep It Clean:**
- No debug code or console.log statements
- No commented-out code
- No TODO comments unless specified in the plan

## Error Handling

### If Implementation Fails

**Compilation / Type Errors:**
- Review the error message carefully
- Check that all imports are correct
- Verify type definitions match the plan
- Ensure function signatures are correct

**Integration Issues:**
- Verify that dependent phases are complete
- Check that integration points are correctly implemented
- Review event/message formats for consistency
- Check that API contracts (HTTP endpoints, service I/O, etc.) match

### If Prerequisites Not Met

**Missing Dependencies:**
- If a prerequisite phase has not been implemented, stop and report to the user
- Do not attempt to implement the prerequisite yourself unless explicitly asked

**Conflicts with Existing Code:**
- Review the conflict carefully
- Resolve in favor of the sub-plan specifications
- Ensure existing functionality is not broken
- Report the conflict and resolution to the user

## Checklist

Before reporting completion, verify:

- [ ] All files from the sub-plan have been modified/created
- [ ] All functions match the specified signatures
- [ ] All types are defined as specified
- [ ] All error handling is implemented
- [ ] All tests specified in the sub-plan are written
- [ ] Type-check / build passes without errors
- [ ] No debug code or console.log statements
- [ ] Code follows project conventions
- [ ] Integration points are correctly implemented
- [ ] No commits were made

## Example Workflow

1. **Receive inputs:**
   - Grand plan: `plans/milestones/feature-grand-plan.md`
   - Sub-plan: `plans/feature-phase-1-backend-service.md`

2. **Read and understand:**
   - Review grand plan for context (what is the overall feature, where does phase 1 fit)
   - Review phase 1 sub-plan for implementation details (files, code snippets, types, tests)

3. **Investigate existing code:**
   - Read the actual source files referenced in the sub-plan
   - Verify file paths and integration points

4. **Implement:**
   - Add the new service method to the file specified in the sub-plan
   - Add the data access method to the file specified in the sub-plan
   - Write unit tests as specified

5. **Validate:**
   - Run the project's build command to verify compilation
   - Review implementation against the sub-plan

6. **Report:**
   - List files created/modified
   - Report build status
   - Note any deviations or issues

## Conclusion

This skill is the implementation counterpart to [plan-split](../plan-split/SKILL.md). Where plan-split produces detailed, actionable sub-plans, this skill executes a single sub-plan faithfully — implementing the code, validating it compiles, and reporting results. It does not commit; the user retains control over version control.
