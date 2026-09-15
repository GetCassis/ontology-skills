---
name: ontology-expansion
description: >-
  Expand an existing Cassis ontology with additional tables, business domains,
  metrics or joins from schema exports, dbt, dashboards and documentation.
  Let the user define the business domain, build on the curated ontology,
  and propose refactors where useful. Produce a Git diff with evidence and
  regression checks. Use for an ontology that is already seeded, including
  projects synced to Git. Does not require live warehouse access. Initial
  ontology creation belongs to the separate ontology-bootstrap skill.
---

# Ontology expansion

Extend the context the team already maintains so its agents can answer more
useful questions. The deliverable is a reviewable change to the existing
ontology, with evidence and tests. Existing definitions are the baseline;
reconstructing them from upstream sources would lose the team's curation.
The existing structure is a starting point, not a boundary on what the user
can model. Keep the bootstrap approach: assemble meaning from evidence, leave
unknowns as questions, and let the user decide business scope and definitions.

## Establish the starting point

Use the user's repository, project and requested scope. Read its instructions
and modeling guide, including the ontology's `AGENTS.md` when present. Discover
the actual ontology path and format from the checkout rather than assuming a
new layout. This skill is standalone; it does not run the bootstrap pipeline.

Before editing, establish:

- The repository, base commit, ontology path and publication branch. Record
  uncommitted changes and preserve them. Use an isolated branch/worktree when
  appropriate; include existing work only when it belongs to the request.
- Whether Git is the publication source. If project access is available, compare
  the published version/commit with the checkout and note pending UI edits.
  Do not export over the checkout, upload a replacement ontology, or publish to
  reconcile a mismatch. If live state cannot be read, prepare the local diff
  and mark publication compatibility as unverified.
- The questions or business area to unlock, available evidence and permitted
  data access. Infer these from the request and repository first. Ask only for
  missing information that would materially change the work.

An expansion request authorizes preparing the changes, not automatically
merging or publishing them. Follow any explicit authorization already given.
In a synced project, a merge to the publication branch can affect all users.

## Let the user define the domain

Read the root rules, relevant domain documents, tables, metrics and joins
before consulting upstream material. Include existing caveats, exclusions,
synonyms, units, time windows and known unsupported concepts.

Compare this context with the supplied schema and source assets. Distinguish:

- Tables present in the schema but not modeled in the ontology.
- Modeled tables with missing columns, relationships or useful definitions.
- Questions that are already covered but are routed or interpreted incorrectly.
- Concepts that cannot be established from the available evidence.

Start from the business area the user wants to model: its purpose, audience,
questions and boundaries. The domain need not match a current folder, schema or
list of unmodeled tables. If it is not yet specified, propose a few useful
candidate domains from the available context and ask the user to choose or
reframe them. Do not silently select one based on table counts or usage alone.
If the user has already defined it, treat that as the starting decision.

Map the chosen domain to existing concepts, missing coverage, required joins
and supporting reference tables. Show what is already represented, what would
be added and where definitions or ownership overlap. Let the user adjust this
scope before substantial enrichment. Do not treat every unmodeled table as
required work or impose a fixed table quota.

### Review the domain structure, including useful refactors

Propose a domain tree for the expanded ontology. Reuse what fits, and consider
refactors when they improve the chosen domain: extracting shared concepts,
splitting a mixed domain, consolidating duplicates, moving misplaced tables or
clarifying names that conflate different metrics. A new business domain can
legitimately span existing technical schemas.

For each proposed refactor, explain the problem it solves, show the current
and proposed structure, and identify affected references, consumers and tests.
Separate structural changes from changes in business meaning. Moving or
renaming a metric must not silently change its formula, filters or unit.
Offer a smaller additive option when the migration cost is disproportionate.

Present the proposed tree and refactors together for review before dependent
edits. Reuse decisions already made in the request; do not ask again. If a
refactor remains undecided, continue additions that do not depend on it.

### Keep the review points about their data

As in bootstrap, the meaningful decisions are the scope, domain tree, metrics
and unresolved questions. Present each as a concise artifact or grouped diff,
with the decision needed first. Batch related metric definitions and conflicts
rather than asking column by column. Do not repeat settled decisions, turn
technical steps into approvals, or invent a business default to move past an
unanswered question. Changes to previously reviewed meaning return to the
user with the consequence made explicit.

## Assemble evidence without recreating the ontology

Use the supplied assets to support the additions:

- A current physical schema establishes table/column identity and types. For
  dbt, use compiled artifacts, catalog/manifest information or an explicit
  mapping where macros and aliases change physical names. A model filename
  alone does not establish the warehouse relation.
- Defining SQL supports grain, transformations, filters and joins. Prefer
  explicit tests/constraints for cardinality; a same-named key does not prove
  uniqueness or prevent fan-out.
- Existing descriptions, governed metric definitions and dashboard SQL provide
  business meaning. Preserve their vocabulary. A dashboard's calculation may
  have a different audience, scope or period from an existing metric.
- Query history and user questions help prioritize coverage; repeated use is
  not proof that a calculation is correct.

Attach a source location to material additions in a review note: repository
path and revision, document link/section, schema snapshot or dashboard/query
reference. Distinguish explicit source statements from inferred descriptions.
Use the ontology's supported provenance fields if they exist; otherwise keep
the evidence outside the serialized ontology so validators can still read it.

Do not silently settle contradictions by replacing a curated definition.
Describe the competing meanings and their effect on the answer. Where two
definitions are valid, preserve them with clear names/scopes and ask for a
default only if one is needed. Ask an owner when the decision changes a number
and the sources cannot settle it. Continue independent additions while waiting.
Keep unsupported metrics out of the governed set; record the missing fact and
the affected question instead of inventing a formula or denominator.

Schema/dbt exports are sufficient for metadata work. If warehouse access is
unavailable or excluded, do not try to connect. Separate SQL prepared for later
validation from SQL actually executed. Agent interpretation still uses the
user's configured model and its data-handling constraints; local files do not
make that interpretation automatically offline.

## Edit the existing tree

Make the agreed additions and refactors in the working branch, using the
current repository format and its authoring guidance. Preserve existing
business rules unless their change has been requested or settled in review.
Use existing deterministic extractors, formatters and validators where they
apply; reserve agent reasoning for mapping evidence to meaning and identifying
conflicts, rather than re-deriving facts a tool can extract.

- Add the required tables/columns with supported descriptions and grain.
- Add only evidenced joins; account for cardinality and aggregation before
  joining. Preserve existing join entries and conditions.
- Reuse governed metrics where applicable. For new ones, make population,
  mandatory filters, time basis, unit and aggregation level explicit.
- Extend domain guidance and navigation to include the new coverage. Preserve
  curated prose and regenerate generated navigation with the repository's
  formatter, if available.
- Preserve identifiers, locations, permissions-related metadata, tests and
  unrelated fields outside the agreed refactor. For a refactor, track old-to-new
  names/paths and update all affected metric, table, join, domain, navigation
  and evaluation references. Check any documented external consumers; report
  those that cannot be checked. Remove replaced objects only as part of the
  agreed migration, and do not leave conflicting copies or broken references.
- Keep refactoring changes distinguishable from new coverage in the diff or
  commits, so the reviewer can assess preserved meaning and added meaning
  separately. Avoid unrelated formatting churn.

Do not point a bootstrap generator at the maintained ontology. If separately
generated candidates are supplied, treat them as evidence to reconcile, not a
replacement tree to copy wholesale. The bootstrap kit's additive dbt export
does not merge an existing Cassis ontology.

Do not write back to dbt, create tracker issues, change project settings or
modify the live schema unless those actions are in the user's request.

## Validate the expansion against its baseline

Inspect the final Git diff. It should account for every added/changed object,
preserve unrelated work, and contain no unexplained deletions or overwritten
business definitions. Check against the recorded base; if the target branch
advanced, reconcile before claiming the change is ready to merge. Resolve the
relationship with pending UI edits before publishing, since Git sync may
replace them.

Use the repository's established validation and the installed CLI's documented
commands. Discover supported flags through help rather than guessing them.
Where available, `cassis ontology fmt` and `cassis ontology check` provide
format/structural checks; inspect their changes and report unavailable checks.
Structural validity does not establish business correctness.

Reuse the existing evaluation suite. Add cases for the new questions and for
existing concepts touched by shared rules, joins or metrics. Where feasible,
compare the baseline and expanded ontology with the same reference cases,
model/settings and data snapshot. Do not rewrite a reference merely to make a
failure pass; establish which definition is authoritative first.

For cases that affect numbers, verify the population, date window, grain,
join multiplication, unit and expected result. Clearly separate:

- Structural/format validation.
- SQL judged equivalent without execution.
- Executed results compared with an accepted reference.

If no data access is authorized, deliver the metadata expansion with its
validation limits and the queries needed for local verification. Do not turn a
SQL-judge pass rate into a claim of result accuracy. Only run remote evaluations
within the user's authorized project, model and data scope.

Existing review issues can supply reproduction cases. Map relevant corrections
to them in the handoff, but do not assume a merged PR closes an issue or that a
resolved status proves the error cannot recur. Update live issue status only
when requested and supported by the applicable workflow and evidence.

## Deliver the review

Return the diff or PR as authorized, plus a concise review note containing:

- The user-defined domain, questions newly covered and objects added or extended.
- Refactors proposed, accepted or deferred, and the reference/migration impact.
- The baseline commit, relevant publication state and evidence for the changes.
- Any changed existing definition, with its reason and agreement.
- Validation actually performed, remaining failures and unresolved questions.
- The next action needed to merge/publish, or confirmation if already authorized
  and completed. Do not label a draft or untested change as production-ready.

Keep review material in the repository's customary location outside the
ontology's serialized objects. A successful expansion grows useful coverage
without erasing the knowledge the team already approved.
