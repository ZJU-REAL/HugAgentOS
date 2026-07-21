# Domain Ontology Governance and Validation

Domain ontology validation adds an optional domain contract to the agent. It checks tools and
workflows before execution and independently reviews high-risk answers after they are generated.
Turning it off preserves the original chat path without ontology injection or gates. Thinking
status, content deltas, and tool events stream in real time whether or not review is activated.

## User Setting

Open **Settings → Ontology Validation** after signing in. CE instance
administrators see this entry as **Ontology Governance**. The same page provides
the personal validation switch and global Domain Pack management. When an
active default Domain Pack is available, enable **Use Domain Ontology
Validation**.

- Off: use the existing flow without ontology injection or gates.
- On with no matching workflow: unrelated domain rules are not applied.
- On with a checkpoint match: one independent reviewer checks the answer after the draft streams.
- On with a committee match: independent reviewers vote to pass, revise, or escalate.

When review starts, the right panel opens automatically and streams validation
status and the revised answer while the original draft remains on the left.
Revision thinking is collapsed by default, and you can expand its tool calls
and new citations. Use the top-right panel button to collapse or reopen the
panel, or use the ontology-validation entry below a historical message to
revisit that result. The system doesn't overwrite the draft until you select
**Replace Original**.

## Domain Packs

A Domain Pack is a JSON domain contract with four layers: concepts, relations,
executable constraints, and workflows. CE instance administrators manage packs
under **Settings → Ontology Governance**. EE administrators continue to use
**Content Management → Ontology Governance**.

An imported version starts as a working draft unless explicitly activated. Each Domain Pack can
have only one working draft, which administrators can update repeatedly. Publishing locks the
draft as an official version and activates it. Activation does not rewrite historical audit
evidence. Fresh databases include an enterprise-risk example pack.

A workflow can define both kinds of entry point:

- `triggers` activates it when the user's text matches a configured phrase.
- `asset_triggers` activates it when an invoked tool, skill, or sub-agent matches an asset ID or a
  governed ontology tag.

Governed asset tags use `ontology:ConceptId`. For example, tagging a risk-query tool or report
skill with `ontology:RiskReport` activates the corresponding workflow when that asset is actually
invoked, even if the user never says a configured phrase such as “risk profile.” Ordinary display
tags do not activate ontology workflows.

### Provide selectable tags

For a tag to appear in the skill or sub-agent selector, the Domain Pack must declare both the
concept and its asset-trigger relationship. Defining the concept alone does not make it a runtime
trigger tag.

1. Define the concept in `concepts`, such as `RiskReport`.
2. Select the asset kind in the target workflow's `asset_triggers`, and add
   `ontology:RiskReport` to `tags_any`.
3. Activate the Domain Pack version. The skill and sub-agent forms then display the tag, linked
   workflows, and review levels.

The following example makes one tag available to both skills and sub-agents:

```json
{
  "concepts": [
    {"id": "RiskReport", "name": "Risk report", "definition": "Enterprise risk report"}
  ],
  "workflows": [
    {
      "id": "enterprise_risk_analysis",
      "review_level": "committee",
      "asset_triggers": [
        {"kind": "skill", "tags_any": ["ontology:RiskReport"]},
        {"kind": "subagent", "tags_any": ["ontology:RiskReport"]}
      ]
    }
  ]
}
```

## Runtime Validation

Tool calls first pass through a deterministic gate that does not use an LLM. The gate can enforce
JSON Schema, prerequisite tools, and forbidden tools. A denial returns the exact reason and a
correction hint to the agent. Repeated denials require a strategy change and eventually trip a
circuit breaker.

Runtime policy escalates monotonically. A turn can move from no match to checkpoint and then to
committee, but it cannot downgrade within the same run. An asset must be invoked to activate its
workflow; merely appearing in the catalog is not enough. The chat displays active workflows,
activation sources, tool-gate results, and committee progress, and the backend stores them as
audit evidence.

Each governed turn receives one `governance_run_id`. The parent agent, children
created through `call_subagent`, and their tools share the same runtime object.
The same `pack_id + workflow_id` is activated once, whether text matches it
first or an asset tag matches it later. Every governed tool call still passes
its own deterministic gate because parameters and prerequisite evidence can
differ. The outer workflow exclusively claims final-answer review, and a child
doesn't start its own committee. Successful child tool calls join the outer
evidence trace with their `agent_id`, `sub_run_id`, and `parent_tool_id`, and
produce the same traceable citations as ordinary tool calls. A trusted child
can therefore satisfy required workflow tools without the outer trace
incorrectly reducing the execution to `call_subagent` alone.

High-risk output also checks required workflow tools, content format, and citation evidence. The
committee runs only for workflows marked as high-risk in a Domain Pack; it is never invoked for
every tool call.

The system runs deterministic output checks before the only checkpoint or
committee review round. If required tools, citations, or output structure are
missing, it sends structured violations back to the original agent first. The
agent continues in the same ReAct context, searches for missing evidence, calls
real tools, and returns one complete revised answer. The revision instruction
requires an evidence-gap comparison. If a new claim needs external facts, the
agent must retrieve them instead of only paraphrasing the draft.

Each answer can produce at most one automatic revision and runs only one
independent review round. When the committee returns `revise`, the original
agent can use the remaining revision opportunity, but the system doesn't start
a second committee loop. Revision thinking streams into a separate, collapsed
thinking area. Tool calls, tool results, and revised content use distinct event
channels and don't enter the original answer.

When human judgment is still required, the backend returns structured
`manual_review` JSON with the affected claim, rule ID, risk, and required human
check. The frontend renders these fields as **Domain Ontology Human Review**
cards instead of appending notice text to the answer. The revision remains
available as a working draft, but pending items aren't presented as
domain-approved conclusions.

The original draft stays on the left. The revised answer, collapsed thinking,
additional tool calls, and human-review cards appear in the right panel, which
follows incoming SSE deltas to the bottom. If you scroll up, the panel stops
following so it doesn't interrupt reading. The system doesn't overwrite the
draft automatically. After you select **Replace Original**, it atomically
updates the message and preserves its accepted state in chat history.

The same setting applies to regular chats, sub-agents, channel bots, automation, plans, batch
items, and autonomous loops. If the user opted in but the runtime policy cannot be built, the run
stops with an error instead of silently bypassing validation.

When a user explicitly selects an `@sub-agent` in the composer, or enters the
strict Chinese command syntax `调用` or `请调用` + one unique complete sub-agent
name + an explicit task, the turn goes directly to that target. The main agent
doesn't run the domain workflow and dispatch a second child first. The request
boundary still creates the ontology runtime and activates it from the
sub-agent ID or tags. Skills and tools invoked inside the target share that
runtime, pass deterministic gates, and complete one checkpoint or committee
review at the highest level reached in the turn.

## Build-time Validation

Skills, MCP tools, plugins, and sub-agents are checked against active Domain Packs before they are
created, imported, installed, published, or saved from distillation. The gate validates tool
dependencies, forbidden tools, parameter schemas, and output contracts. Governed vocabulary tags
use `ontology:ConceptId`; unknown concepts are reported before saving, while ordinary tags remain
unrestricted.

When an asset lacks a low-level tool required by a workflow, the validation report looks up the
bindable global MCP servers that provide it. The report displays the MCP and tool display names,
using Chinese names when configured, while retaining raw tool IDs in the complete report for
administrator troubleshooting.

When you create or edit a skill, select MCP services under **Bound Tools (MCP)** instead of
entering low-level tool IDs. The backend reads the MCP's discovered `tools_json` and uses those
tools for build-time validation. A successful save records `ontology_tags`,
`ontology_workflows`, `mcp_servers`, and `allowed_tools` in `SKILL.md`. These bindings remain with
the skill when you edit, export, publish, or import it again. When the skill is enabled, its
declared MCP services are merged into that execution's capability scope without enabling other
unbound MCP services.

The skill and sub-agent forms separate general tags from **Ontology governance tags**. The
ontology selector only lists tags from active Domain Packs that are linked to a workflow through
`asset_triggers.tags_any` for that asset kind. After selection, the form displays the workflows and
review levels that an actual invocation will trigger.

MCP tool tags can come from a tool's `tags` or `ontology_tags`, or from its MCP server
configuration. Skills use governed tags in their own metadata. The same tags participate in
build-time checks and runtime `asset_triggers`.

Administrators can set server-level tags in the MCP editor or assign tool-level tags using
`tool_name=ontology:ConceptId`. User and administrator forms for skills and sub-agents provide a
controlled ontology governance tag selector.

## Governance

CE instance administrators can complete governance under **Settings → Ontology
Governance** without the separate Content Management console, which isn't part
of CE. EE continues to expose **Content Management → Ontology Governance**.
Both entries use the same governance components and service logic, with these
operations:

- import, validate, export, activate, and select default Domain Packs;
- inspect gate events, reviewer evidence, and latency;
- inspect closed-loop metrics for gates, reviews, drafts, candidate merges, and acceptance by source;
- automatically prefilter evolution drafts from repeated denials or review revisions, with a
  manual trigger as well;
- add explicit user corrections on ontology-matched answers to the human-review queue;
- approve or reject evolution candidates and merge structured candidates into the working draft.

### Edit a version from its details

Open a Domain Pack's **Details**, and select **Edit This Module** from the
Overview, Concepts, Relations, Constraints, or Workflows tab. Each editor
changes only its selected module. All other modules are copied from the
version you are viewing.

The editor uses structured forms, so you don't need to write JSON:

- The overview form manages the pack description, injection budget, and
  review thresholds.
- The concept form manages definitions, aliases, hierarchy, tags, and
  controlled values item by item.
- The relation form connects existing concepts and manages predicates,
  cardinality, and forbidden relations.
- The constraint form manages targets, messages, and correction guidance. It
  builds JSON Schema from data types, required fields, enumerations, lengths,
  and numeric ranges.
- The workflow form manages text and asset activation, required and forbidden
  tools, output tags, and review levels.

Before saving, the console validates the assembled Domain Pack and lists each
error at its exact path. The first edit creates one working draft with the
version number you confirm. Later edits to any module update that same draft,
so each save doesn't add another version. If a working draft already exists,
starting an edit from an official or archived version switches to the existing
draft and preserves changes in its other modules.

Publishing locks the working draft as an official version and immediately
replaces the runtime version. Official and archived versions are read-only. To
make more changes, create the next working draft from the version you're
viewing. Version Management displays 10 rows per page by default and lets you
select 20 or 50 rows. Pagination never deletes history; the backend retains all
official versions. You can also discard an unwanted working draft before
publishing it. The Raw JSON tab remains a read-only verification and migration
view.

Evolution never modifies or publishes the production version automatically.
An administrator reviews each candidate, merges an approved structured
candidate into the working draft, and then publishes that draft explicitly.
Conversation evidence and candidates are rejected when classified and are
sanitized for sensitive data and injection content before storage. A candidate
can't be merged twice.

## API

The principal endpoints are listed below. The CE Settings endpoints require
instance-administrator permission, with `ADMIN_TOKEN` as an operational
fallback. EE `/v1/admin/ontologies` endpoints continue to require `ADMIN_TOKEN`
or an equivalent content-management permission.

| Method and path | Purpose |
|---|---|
| `GET/PATCH /v1/ontologies/settings` | Read or update the user opt-in setting |
| `GET /v1/ontologies/runtime/preview` | Preview rules matched by a task |
| `GET /v1/ontologies/tags?asset_kind=skill` | List selectable asset-trigger tags |
| `GET /v1/ontologies/governance/access` | Check whether the current CE user can manage global ontology assets |
| `/v1/ontologies/governance/*` | Manage CE packs, versions, evidence, and evolution from Settings |
| `GET /v1/admin/ontologies` | List Domain Packs and versions |
| `GET /v1/admin/ontologies/tags?asset_kind=skill` | List administrator asset-trigger tags |
| `GET /v1/admin/ontologies/metrics` | Inspect closed-loop governance metrics |
| `POST /v1/admin/ontologies/validate` | Validate Domain Pack JSON |
| `POST /v1/admin/ontologies/versions` | Import a new version |
| `PUT /v1/admin/ontologies/{pack_id}/draft` | Create or update the only working draft |
| `DELETE /v1/admin/ontologies/{pack_id}/draft/{version_id}` | Discard an unpublished working draft |
| `POST .../activate` | Publish a draft or activate a historical version |
| `POST /v1/admin/ontologies/build/validate` | Preflight a skill, tool, or sub-agent asset |
| `GET .../events`, `GET .../reviews`, `GET .../drafts` | Inspect runtime and evolution evidence |
| `POST /v1/chats/messages/{message_id}/ontology-revision/accept` | Replace the original message with its ontology revision |

## Operations

Run the database migration before starting a release that contains this feature.
If the user page shows that no pack is available, a CE instance administrator
must verify under **Settings → Ontology Governance** that a pack is enabled,
selected as the default, and has an active version. EE administrators perform
the same check in Content Management.

Bundled enterprise-risk updates preserve immutable versions. A fresh database activates the
latest bundled version. When an existing database has no working draft, it stages the update as a
draft and keeps the currently active version. If a working draft exists, staging waits until that
draft is published or discarded. An administrator must review and explicitly publish the staged
draft before its asset triggers enter runtime.
