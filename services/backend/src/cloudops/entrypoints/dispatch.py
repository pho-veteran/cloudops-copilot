"""Trusted job-kind to permit-class mapping for every entrypoint that reads DispatchPermit/v1.

Concrete kinds keep their historical spellings; a kind absent here can never be dispatched.
"""

from cloudops.shared.dispatch_permit import DispatchGate

PERMIT_CLASSES = {
    "diagnosis_analysis_v1": "analysis",
    "diagnosis_probe_v1": "analysis",
    "development_collection_v1": "collection",
    "recommendation_report_v1": "recommendation",
    "recovery_execution_v1": "recovery_execution",
    "recovery_reconciliation_v1": "collection",
}
CLASSES = sorted(set(PERMIT_CLASSES.values()))
GATE = DispatchGate(PERMIT_CLASSES)
