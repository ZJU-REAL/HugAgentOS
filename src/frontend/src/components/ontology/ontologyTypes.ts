export interface ValidationIssue {
  severity?: string;
  path?: string;
  message?: string;
}

export interface OntologyPackVersion {
  version_id: string;
  version: string;
  status: 'draft' | 'active' | 'retired';
  checksum: string;
  validation_report: {
    valid?: boolean;
    errors?: ValidationIssue[];
    warnings?: ValidationIssue[];
  };
  created_at?: string;
  updated_at?: string;
  activated_at?: string;
}

export interface OntologyPackSummary {
  pack_id: string;
  name: string;
  domain: string;
  description: string;
  is_enabled: boolean;
  is_default: boolean;
  active_version_id?: string | null;
  working_draft_version_id?: string | null;
  versions: OntologyPackVersion[];
}

export interface OntologyConcept {
  id: string;
  name: string;
  aliases?: string[];
  definition: string;
  parent_id?: string | null;
  closed_values?: string[];
  tags?: string[];
  risk?: string;
}

export interface OntologyRelation {
  id: string;
  subject: string;
  predicate: string;
  object: string;
  description?: string;
  min_cardinality?: number | null;
  max_cardinality?: number | null;
  forbidden?: boolean;
}

export interface OntologyConstraintTarget {
  kind: 'tool' | 'tool_parameter' | 'output';
  tool?: string | null;
  parameter?: string | null;
  output_tag?: string | null;
}

export interface OntologyConstraint {
  id: string;
  name: string;
  target: OntologyConstraintTarget;
  schema?: Record<string, unknown>;
  concept_id?: string | null;
  requires_citations?: boolean;
  prerequisite_tools?: string[];
  mode?: string;
  risk?: string;
  message: string;
  suggestion?: string;
  enabled?: boolean;
}

export interface OntologyAssetTrigger {
  kind: 'tool' | 'skill' | 'subagent';
  ids?: string[];
  tags_any?: string[];
}

export interface OntologyWorkflow {
  id: string;
  name: string;
  triggers?: string[];
  asset_triggers?: OntologyAssetTrigger[];
  required_tools?: string[];
  forbidden_tools?: string[];
  output_tags?: string[];
  review_level?: string;
  risk?: string;
}

export interface OntologyPackConfig {
  injection_enabled?: boolean;
  max_concepts?: number;
  token_budget?: number;
  committee_size?: number;
  repeated_denial_threshold?: number;
  circuit_breaker_threshold?: number;
  allow_unresolved_tools?: boolean;
}

export interface OntologyDocument {
  schema_version?: string;
  pack_id: string;
  name: string;
  version: string;
  domain: string;
  description?: string;
  config?: OntologyPackConfig;
  concepts?: OntologyConcept[];
  relations?: OntologyRelation[];
  constraints?: OntologyConstraint[];
  workflows?: OntologyWorkflow[];
}

export type OntologyEditableModule =
  | 'overview'
  | 'concepts'
  | 'relations'
  | 'constraints'
  | 'workflows';
