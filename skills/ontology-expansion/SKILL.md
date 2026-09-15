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
- Which validation can actually run here, before drafting rather than after.
  Run each check the repository and the installed CLI offer once, now, on the
  untouched baseline: the structural check, the formatter, the repository's own
  test or eval command. Some need a project binding or credentials this
  checkout does not have, and a plan that leans on an eval suite nobody can run
  is a plan with no behavioural check in it. Say at the outset which checks are
  available and what will therefore stay unverified.

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
If the user has already defined it, treat that as the starting decision. If the
user cannot be reached, work the best-evidenced candidate and name the ones you
set aside, with what each would have unlocked, at the top of the review.

Map the chosen domain to existing concepts, missing coverage, required joins
and supporting reference tables. Include what is already represented, what is
being added and where definitions or ownership overlap in the review. Do not
treat every unmodeled table as required work or impose a fixed table quota.

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

**Get the structure approved before authoring any content.** Present the
proposed tree — new domains, splits, moves, and the table that lands in each —
and wait. Every table and metric names a domain, so content filled into a
hierarchy the user then changes all has to move. This is the one approval the
expansion asks for: once the tree is agreed, draft everything the evidence
supports without stopping for further scope or structure approval, and come
back with the first version plus what is ambiguous or needs their input.

If the user cannot be reached, draft against the tree you would have proposed,
say so, and put the tree first in the review note so the reviewer reads the
structure before the content it determines. If a refactor remains undecided,
continue the additions that do not depend on it.

### Keep the review points about their data

Collect ambiguities in the draft or PR review note: the affected concept,
competing interpretations, supporting evidence and clarification needed. Leave
the uncertain definition unchanged or omit the unsupported addition while
drafting the rest. Ask during drafting only when the ambiguity prevents useful
progress on the chosen scope; otherwise batch questions for review of the
concrete draft. Do not invent a business default to move past an unanswered
question. Changes to previously reviewed meaning return to the user with the
consequence made explicit.

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
default only if one is needed.

**Changing something the team already approved: apply it when the evidence is
strong, propose it when it is not, flag it either way.** The evidence is strong
when the expansion makes an existing statement false — a root convention that
was true of the old coverage and is wrong for the new tables — or when the
sources settle the disagreement outright. Then make the change, keep it in its
own commit so it can be read and reverted on its own, and say in the review
what it was, why, and what moves if the user disagrees. When the sources cannot
settle it, or when the change is about where a term routes rather than about a
statement now being false, leave it and propose it. Never let either kind reach
the user only as a line in a diff, and never bundle it into a commit of
additions. Defer the unsettled definition and continue independent additions.
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
- A calculation the metric format cannot hold still needs a route. When the
  business asked for a number and the answer is a recipe rather than a metric —
  two facts on different clocks, a pre-aggregate before a join that fans out —
  write the recipe where the answering agent reads it, which is the domain's
  own prose, and add the case to the evaluation suite. Recording it only in the
  review note leaves the question unanswerable for everyone but the reviewer.
  Say in the review why it is not a metric.
- Extend domain guidance and navigation to include the new coverage. Preserve
  curated prose. Generated navigation regions are regenerated by the
  repository's formatter — `cassis ontology fmt` for a Cassis tree, which also
  refreshes a domain README's nav region; run it rather than hand-writing those
  blocks, and note that a stale one fails `cassis ontology check`.
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

Reuse the existing evaluation suite, within the limits you established at the
outset. Add cases for the new questions and for
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
