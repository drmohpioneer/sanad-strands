"""Single-use invitations; QR pixels are rendered only at transport time."""

import io

import segno

from sanad.auth.claim import ClaimService
from sanad.auth.commands import IssuedInvitation, IssueInvitation
from sanad.domain import Principal
from sanad.scribe.policy import DRAFT_SCRIBE_POLICY
from sanad.store.records import CommitResult


def render_png(url: str) -> bytes:
    stream = io.BytesIO()
    segno.make_qr(url, error="m").save(stream, kind="png", scale=DRAFT_SCRIBE_POLICY.qr_scale)
    return stream.getvalue()


def issue_qr(
    claims: ClaimService,
    actor: Principal,
    patient_id: str,
    command_id: str,
) -> IssuedInvitation | CommitResult:
    return claims.issue_invitation(
        IssueInvitation(
            command_id=command_id,
            actor=actor,
            patient_id=patient_id,
            include_qr=True,
        )
    )
