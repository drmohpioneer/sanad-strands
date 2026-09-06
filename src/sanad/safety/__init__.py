"""Public deterministic kernel for the complete readable text/caption input.

Unknown is never normal; a missing protocol is not a safe result. Hidden
unprocessed media belongs to the caller's media_failure route. A verdict is not
a diagnosis. Original adult cardiology policy: Sanad v2 re-approval pending.

Only this policy-explicit API is for new callers. Copied module functions retain
historical behavior for regression/provenance and are not the v2 safety boundary.
"""

from sanad.safety._provenance import REUSED_MODULES
from sanad.safety.kernel import (
    find_bp,
    grade_bp,
    grade_lab,
    normalize,
    render_urgent,
    screen_text,
    to_incident_facts,
    validate_patient_output,
    wants_treatment_change,
)
from sanad.safety.models import (
    IncidentFacts,
    LabCandidate,
    LabVerdict,
    OrderSummary,
    OutputContext,
    Quantity,
    ScreenVerdict,
    ValidationVerdict,
    Violation,
    VitalVerdict,
)
from sanad.safety.policy import (
    SAFETY_POLICY_V1_CARDIOLOGY_DRAFT,
    BpThresholds,
    LabRule,
    SafetyPolicy,
)

__all__ = [
    "REUSED_MODULES",
    "find_bp",
    "grade_bp",
    "grade_lab",
    "normalize",
    "render_urgent",
    "screen_text",
    "to_incident_facts",
    "validate_patient_output",
    "wants_treatment_change",
    "IncidentFacts",
    "LabCandidate",
    "LabVerdict",
    "OrderSummary",
    "OutputContext",
    "Quantity",
    "ScreenVerdict",
    "ValidationVerdict",
    "Violation",
    "VitalVerdict",
    "BpThresholds",
    "LabRule",
    "SafetyPolicy",
    "SAFETY_POLICY_V1_CARDIOLOGY_DRAFT",
]
