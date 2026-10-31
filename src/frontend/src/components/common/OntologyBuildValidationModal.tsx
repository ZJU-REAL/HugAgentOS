import type { OntologyBuildFailure } from '../../utils/apiError';

interface OntologyBuildValidationModalProps {
  failure: OntologyBuildFailure | null;
  onClose: () => void;
}

/** Community edition never rejects a build on ontology rules, so there is nothing to show. */
export function OntologyBuildValidationModal(_props: OntologyBuildValidationModalProps) {
  return null;
}
