"""Ontology harness: Domain Pack validation, runtime selection, and gates."""

from core.ontology.schemas import OntologyPackDocument
from core.ontology.validator import DomainPackValidator, OntologyGateDecision

__all__ = ["DomainPackValidator", "OntologyGateDecision", "OntologyPackDocument"]
