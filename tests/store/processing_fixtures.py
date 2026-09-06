from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

from domain_fixtures import NOW, ORDER, POLICY, mission
from harness import FakeClock
from pydantic import BaseModel, JsonValue

from sanad.channels.transport import CapturedTransport
from sanad.domain import Mission, MissionState, PatientScope, Principal, VersionRef
from sanad.steward.apply import make_intent
from sanad.steward.dispatch import Dispatcher
from sanad.steward.inbound import InboundProcessor
from sanad.steward.service import Steward
from sanad.steward.sweep import SweepBudget, Sweeper
from sanad.steward.types import StewardPolicy, records
from sanad.steward.urgent import UrgentService
from sanad.store._base import StoreBase
from sanad.store.records import (
    Accepted,
    CommandEnvelope,
    CommitRequest,
    DoctorAuthority,
    InboundReceipt,
    OrderAuthority,
    OutboundIntent,
    PatientProfile,
    WorkerCapability,
    from_record,
    model_scope,
    to_record,
)
from store.fixtures import SCOPE, TENANT

DOCTOR = Principal(
    subject="synthetic-doctor",
    actor_kind="doctor",
    doctor_id=SCOPE.doctor_id,
    verified_roles=frozenset({"doctor"}),
    auth_epoch=0,
)
PATIENT = Principal(
    subject="synthetic-patient-subject",
    actor_kind="patient",
    doctor_id=SCOPE.doctor_id,
    patient_id=SCOPE.patient_id,
    verified_roles=frozenset({"patient"}),
    auth_epoch=0,
)


@dataclass
class World:
    store: StoreBase
    clock: FakeClock
    policy: StewardPolicy
    steward: Steward
    inbound: InboundProcessor
    transport: CapturedTransport
    dispatcher: Dispatcher
    sweep: Sweeper
    urgent: UrgentService

    @classmethod
    def create(cls, store: StoreBase, clock: FakeClock) -> "World":
        policy = StewardPolicy(POLICY)
        steward = Steward(store, clock, lambda scope: policy)
        transport = CapturedTransport()
        inbound, dispatcher = InboundProcessor(steward), Dispatcher(steward, transport)
        cap = WorkerCapability(
            service_subject="synthetic-sweep",
            permitted_lanes=frozenset({"mission", "followup", "review", "ingress", "delivery"}),
            resolved_scope=SCOPE,
            auth_expiry=NOW + timedelta(days=365),
            invocation_id="synthetic-sweep",
        )
        world = cls(
            store,
            clock,
            policy,
            steward,
            inbound,
            transport,
            dispatcher,
            Sweeper(steward, inbound, dispatcher, cap),
            UrgentService(steward, patient_template_id="synthetic-safety-v1"),
        )
        world.put(
            DoctorAuthority(
                id=SCOPE.doctor_id,
                doctor_id=SCOPE.doctor_id,
                subject=DOCTOR.subject,
                approved=True,
                auth_epoch=0,
                recipient_ref="synthetic-doctor-chat",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        world.put(
            PatientProfile(
                id=SCOPE.patient_id,
                patient_id=SCOPE.patient_id,
                doctor_id=SCOPE.doctor_id,
                created_at=NOW,
                updated_at=NOW,
                binding_active=True,
                consent_active=True,
                consent_version=1,
                recipient_ref="synthetic-patient-chat",
                recipient_subject=PATIENT.subject,
            )
        )
        world.put(
            OrderAuthority(
                id=ORDER.id, scope=SCOPE, status="active", created_at=NOW, updated_at=NOW
            )
        )
        return world

    def put(self, *models: BaseModel) -> None:
        scope = model_scope(models[0])
        fence = None
        if isinstance(scope, PatientScope) and self.store.get_patient_profile(scope) is not None:
            fence = self.store.acquire_patient(
                scope, "synthetic-fixture", self.clock(), self.policy.operations.lease_ttl
            )
            assert fence is not None
        try:
            # A fixture profile revision preserves the lease just acquired.
            prepared = []
            for model in models:
                if isinstance(model, PatientProfile) and fence is not None:
                    model = PatientProfile.model_validate(
                        model.model_dump()
                        | {
                            "lease_owner": fence.owner,
                            "lease_expires_at": fence.expires_at,
                            "lease_generation": fence.generation,
                            "version": self.profile.version + 1,
                        }
                    )
                prepared.append(to_record(model, scope))
            command = CommandEnvelope(
                command_id="fixture:" + uuid4().hex,
                principal=DOCTOR,
                scope=scope,
                payload={"synthetic": True},
                requested_at=self.clock(),
                fence=fence,
            )
            result = self.store.commit(
                CommitRequest(
                    command=command, puts=tuple(prepared), expected=tuple(r.ref for r in prepared)
                )
            )
            assert isinstance(result, Accepted), result
        finally:
            if fence is not None:
                self.store.release_patient(fence)

    @property
    def profile(self) -> PatientProfile:
        profile = self.store.get_patient_profile(SCOPE)
        assert profile is not None
        return profile

    @property
    def doctor(self) -> DoctorAuthority:
        row = self.store.get(TENANT, "doctor_authority", SCOPE.doctor_id)
        assert row is not None
        return from_record(row, DoctorAuthority)

    def mission(self, id: str = "synthetic-mission") -> Mission:
        value = self.store.get_mission(SCOPE, id)
        assert value is not None
        return value

    def receipt(self, id: str) -> InboundReceipt:
        row = self.store.get(SCOPE, "inbound_receipt", id)
        assert row is not None
        return from_record(row, InboundReceipt)

    def command(
        self,
        kind: str,
        *,
        principal: Principal = DOCTOR,
        id: str | None = None,
        target: str = "mission_id",
        target_id: str = "synthetic-mission",
        **payload: Any,
    ) -> CommandEnvelope:
        data: dict[str, JsonValue] = {"type": kind, **payload}
        expected: tuple[VersionRef, ...] = ()
        if kind != "SetContactPreference":
            data[target] = target_id
            row = self.store.get(SCOPE, target.removesuffix("_id"), target_id)
            if row:
                expected = (row.ref,)
        return CommandEnvelope(
            command_id=id or "command:" + uuid4().hex,
            principal=principal,
            scope=SCOPE,
            payload=data,
            expected_versions=expected,
            requested_at=self.clock(),
        )

    def confirm(self, **changes: object) -> Mission:
        self.put(mission(MissionState.proposed, **changes))
        assert self.steward.handle(self.command("ConfirmProposal")).status == "accepted"
        return self.mission(str(changes.get("id", "synthetic-mission")))

    def queued(self, purpose: str | None = None) -> list[OutboundIntent]:
        return [
            from_record(r, OutboundIntent)
            for r in records(self.store, SCOPE, "outbound_intent")
            if r.body["status"] == "queued"
            and (purpose is None or r.body["notification_purpose"] == purpose)
        ]

    def prompt(
        self, *, slot: str = "synthetic-day-chase", source: Mission | None = None
    ) -> OutboundIntent:
        source = source or self.mission()
        intent = make_intent(
            SCOPE,
            "synthetic-prompt:" + uuid4().hex,
            (to_record(source, SCOPE).ref,),
            "routine_prompt",
            "synthetic-payload",
            self.clock(),
            self.policy,
            self.doctor,
            self.profile,
            audience="patient",
            slot=slot,
            order_refs=source.order_refs,
        )
        self.put(intent)
        return intent

    def dispatch(self, intent: OutboundIntent) -> OutboundIntent:
        result = self.dispatcher.dispatch_one(
            to_record(intent, SCOPE).scoped_key(SCOPE), "synthetic-dispatcher", self.clock()
        )
        assert result is not None
        return result

    def run_sweep(self, lane: str, **budget: Any) -> Any:
        return self.sweep.sweep(lane, "0", self.clock(), SweepBudget(**budget))

    def accept(self, payload: dict[str, JsonValue], key: str = "synthetic-update") -> Any:
        result = self.inbound.accept(
            key,
            payload,
            SCOPE,
            self.clock(),
            principal=PATIENT,
            source_chat="synthetic-patient-chat",
        )
        assert result.record is not None
        return result
