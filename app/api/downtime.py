"""Go to downtime: packets per patient, packet view, policy answers."""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from app import config, sim_client
from app.ai import packets, policy
from app.api import actor_role
from app.api.snapshot import evaluate_rules, pull_and_store
from app.rules import snapshot_alerts
from app.store import get_store
from contracts import DowntimePacket, PatientSnapshot, PolicyAnswer

router = APIRouter(tags=["downtime"])


class DowntimeStart(BaseModel):
    simulate_outage: bool = True


class PolicyQuestion(BaseModel):
    question: str


@router.post("/downtime/start", response_model=list[DowntimePacket])
def downtime_start(body: DowntimeStart | None = Body(default=None), role: str = Depends(actor_role)) -> list[DowntimePacket]:
    store = get_store()
    patients = store.latest_snapshot()
    if not patients:
        try:
            patients = pull_and_store(role)
        except sim_client.SimUnavailable as exc:
            raise HTTPException(409, "no snapshot stored and the BCA feed is unreachable") from exc
    now = sim_client.now()
    if body is None or body.simulate_outage:
        sim_client.outage_start()
    evaluate_rules(patients, now, role, "downtime.start")

    def build(p: PatientSnapshot) -> DowntimePacket:
        packet = packets.generate_packet(p, snapshot_alerts(p, now), now, actor_role=role)
        store.save_packet(packet)
        return packet

    # boto3 is synchronous: run the per-patient generations on a bounded worker pool (at most
    # PACKET_CONCURRENCY Bedrock calls in flight). Each ai.call still logs its own cost and latency.
    with ThreadPoolExecutor(max_workers=config.packet_concurrency(), thread_name_prefix="packet") as pool:
        out = list(pool.map(build, patients))
    out.sort(key=packets.priority)
    store.set_kv("phase", "downtime")
    store.set_kv("outage_started_at", now.isoformat())
    # Random per-outage id: every reseed restarts the unit clock at 02:45, so outage_started_at repeats
    # across rehearsals and cannot key per-outage device state (role checklist check marks).
    store.set_kv("outage_id", uuid.uuid4().hex)
    store.audit(role, "downtime.start", "unit", None, {"packets": len(out), "now": now.isoformat()})
    return out


@router.get("/packets", response_model=list[DowntimePacket])
def list_packets(role: str = Depends(actor_role)) -> list[DowntimePacket]:
    store = get_store()
    store.audit(role, "packet.list", "packet")
    return sorted(store.list_packets(), key=packets.priority)


@router.get("/patients/{patient_id}/packet", response_model=DowntimePacket)
def get_packet(patient_id: str, role: str = Depends(actor_role)) -> DowntimePacket:
    store = get_store()
    packet = store.get_packet(patient_id)
    store.audit(role, "packet.view", "packet", patient_id, {"found": packet is not None})
    if packet is None:
        raise HTTPException(404, f"no packet for {patient_id}; POST /downtime/start first")
    return packet


@router.post("/policy/ask", response_model=PolicyAnswer)
def policy_ask(body: PolicyQuestion, role: str = Depends(actor_role)) -> PolicyAnswer:
    answer = policy.ask(body.question, actor_role=role)
    get_store().audit(role, "policy.ask", "policy", ",".join(c.section_id for c in answer.citations) or None,
                      {"question": body.question, "grounded": answer.grounded, "model_id": answer.model_id})
    return answer
