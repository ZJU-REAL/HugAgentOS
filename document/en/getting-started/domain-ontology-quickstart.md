# Build a domain ontology quickly

> Last updated: July 21, 2026 |
> [简体中文](../../zh-CN/getting-started/domain-ontology-quickstart.md)

This guide helps you build and publish a first Domain Pack in about 20 minutes.
HugAgentOS then uses its controlled concepts, relationships, constraints, and
workflows to validate domain tasks. The example has no external MCP dependency,
so you can verify the complete loop before binding production tools and skills.

## What a Domain Pack contains

A HugAgentOS domain ontology is an executable business contract, not only a
glossary for retrieval. A Domain Pack contains these parts:

- **Metadata and configuration** define the pack ID, version, injection budget,
  and circuit-breaker thresholds.
- **Concepts** define domain objects, aliases, meanings, hierarchies, controlled
  values, and risk levels.
- **Relationships** define allowed or forbidden links and cardinality rules.
- **Constraints** apply JSON Schema, evidence, and prerequisite requirements to
  tool arguments or final outputs.
- **Workflows** define text and asset triggers, required or forbidden tools,
  output tags, and review levels.

> [!TIP]
> Keep your first pack focused on one high-value task. Start with two to five
> concepts, one workflow, and one executable constraint, then expand it after
> the full loop works.

## Step 1: choose a testable task

Write the business goal as one sentence that you can test. For example:
“Analyze supplier risk in a procurement request and deliver a complete risk
summary.” Then define these three elements:

1. List the objects the task must understand, such as a procurement request
   and supplier risk.
2. State the invariant the result must satisfy, such as a minimum amount of
   information in the risk summary.
3. Select a review level: `none` adds no review, `checkpoint` uses one reviewer,
   and `committee` uses multiple reviewers for high-risk work.

## Step 2: import a minimal Domain Pack

Sign in as an instance administrator, open **Settings → Ontology Governance**,
and select **Import Domain Pack**. Paste the JSON below. This example activates
on procurement-risk text or a governed skill tag, then requires the final
summary to contain at least 120 characters.

```json
{
  "schema_version": "1.0",
  "pack_id": "procurement_risk",
  "name": "Procurement Risk Domain Pack",
  "version": "1.0.0",
  "domain": "procurement-risk",
  "description": "Governs supplier-risk identification and summaries for procurement requests.",
  "config": {
    "injection_enabled": true,
    "max_concepts": 8,
    "token_budget": 1600,
    "committee_size": 3,
    "repeated_denial_threshold": 2,
    "circuit_breaker_threshold": 5,
    "allow_unresolved_tools": false
  },
  "concepts": [
    {
      "id": "ProcurementRequest",
      "name": "Procurement Request",
      "aliases": ["Purchase Request"],
      "definition": "A business request containing the purchase, budget, requesting unit, and candidate suppliers.",
      "tags": ["procurement"],
      "risk": "low"
    },
    {
      "id": "SupplierRisk",
      "name": "Supplier Risk",
      "aliases": ["Supply Risk"],
      "definition": "A risk that can affect a supplier's delivery, compliance, or continued operation.",
      "closed_values": ["low", "medium", "high", "unverified"],
      "tags": ["risk"],
      "risk": "medium"
    }
  ],
  "relations": [
    {
      "id": "request_has_supplier_risk",
      "subject": "ProcurementRequest",
      "predicate": "has",
      "object": "SupplierRisk",
      "description": "A procurement request can be associated with one or more supplier risks.",
      "min_cardinality": 0,
      "forbidden": false
    }
  ],
  "constraints": [
    {
      "id": "procurement_summary_complete",
      "name": "Procurement risk summary must be complete",
      "target": {
        "kind": "output",
        "output_tag": "procurement_risk_summary"
      },
      "schema": {
        "type": "string",
        "minLength": 120
      },
      "concept_id": "SupplierRisk",
      "requires_citations": false,
      "prerequisite_tools": [],
      "mode": "enforce",
      "risk": "medium",
      "message": "The procurement risk summary is too short for business review.",
      "suggestion": "Add risk facts, impact, unknowns, and recommended verification steps.",
      "enabled": true
    }
  ],
  "workflows": [
    {
      "id": "procurement_risk_review",
      "name": "Procurement Risk Review",
      "triggers": [
        "procurement risk",
        "supplier risk",
        "采购风险",
        "供应商风险"
      ],
      "asset_triggers": [
        {
          "kind": "skill",
          "tags_any": ["ontology:SupplierRisk"]
        }
      ],
      "required_tools": [],
      "forbidden_tools": [],
      "output_tags": ["procurement_risk_summary"],
      "review_level": "checkpoint",
      "risk": "medium"
    }
  ]
}
```

Leave **Publish immediately after import** off, then select **Validate and
Import**. HugAgentOS checks identifiers, versions, concept references, JSON
Schema, and tool references before it creates the working draft. Validation
errors include the exact field path.

## Step 3: review and publish the version

Review the working draft before it enters the runtime. Complete these steps in
order:

1. Open **Details** and inspect the overview, concepts, relationships,
   constraints, and workflows.
2. Open **Version Management** and publish the `1.0.0` working draft.
3. Return to the pack list and turn on **Enabled**.
4. If this pack must be the runtime default, turn on **Default**.

A published version becomes read-only. Later edits create a new working draft
and don't overwrite the active version.

## Step 4: enable and test ontology validation

In **Settings → Ontology Governance**, turn on **Use domain ontology
validation**. Start a new chat and enter this test prompt:

```text
Analyze supplier risk in this procurement request and provide a risk summary.
```

When the request matches `procurement risk` or `supplier risk`, the chat shows
a workflow-activation event and runs a `checkpoint` review after the answer.
The Ontology Governance page records the gate event and review result.

Run two negative tests to confirm that the policy is scoped correctly:

- Ask an unrelated question and confirm that the workflow doesn't activate.
- Request an extremely short procurement-risk conclusion and confirm that the
  output constraint is reported or a revision is produced.

## Step 5: bind production tools and skills

After the minimal example works, extend the ontology from output validation to
action gating. First confirm the real tool IDs and argument schemas in MCP
management, then make these changes:

1. Add every mandatory tool to the workflow's `required_tools`.
2. Set `target.tool` on a constraint to a real tool ID, and constrain its input
   with `schema`.
3. Add tools to `prerequisite_tools` when the workflow requires an execution
   order.
4. Activate workflows by tool ID or `ontology:ConceptId` in `asset_triggers`.
5. Bind the same controlled tag through the **Ontology governance tags**
   selector when you edit a skill or sub-agent.

Validation fails by default when a pack references an unregistered tool. Set
`allow_unresolved_tools` to `true` only during a staged migration, and restore
strict validation before production publication.

## Pre-publication checklist

A production-ready Domain Pack must pass at least these checks:

- Every concept has a precise definition, and aliases don't conflict.
- Every relationship references existing concepts, and the hierarchy has no
  cycles.
- Tool constraints use real tool IDs and argument names from the runtime.
- Every workflow has at least one text trigger or asset trigger.
- Required and forbidden tool sets don't overlap.
- Ordinary prompts don't activate the pack, while high-risk prompts reach the
  intended review level.
- Violation messages explain why execution failed, and repair guidance tells
  the agent what to do next.
- Administrators test a working draft before explicitly publishing it.

## Troubleshooting

The following table covers the most common problems in a first Domain Pack.

| Symptom | Resolution |
|---|---|
| `unknown tool reference` | Confirm the discovered MCP tool ID, or remove the tool dependency until the minimal loop works. |
| A tag is missing from the skill selector | Define its concept and reference the tag in `asset_triggers.tags_any` for that asset kind. |
| A chat doesn't activate the workflow | Confirm that the pack is published, enabled, and default, then check whether a `triggers` value appears in the prompt. |
| Runtime behavior doesn't change after an edit | Published versions are read-only. Edit a working draft and publish the new version. |
| Rules block too many requests | Narrow the triggers, lower the mode or risk level, and use audit records to identify false matches. |

## Next steps

Read [Domain Ontology Governance and Validation](../modules/ontology-harness.md)
for tool gates, asset tags, committee review, version governance, and governed
evolution. To bind external capabilities, continue with the
[MCP Tool System](../modules/mcp-tools.md) and
[Agent Skills](../modules/agent-skills.md).
