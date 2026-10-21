"""G3: explicit Phase 1 transitions. No timers, filesystem or assistant actions."""
from enum import StrEnum
import re
from types import MappingProxyType

from . import store
from .store import DeliveredHashMismatch, InvalidTransition, VersionMismatch


class State(StrEnum):
    DETECTED = 'DETECTED'
    CAPTURED = 'CAPTURED'
    PENDING_APPROVAL = 'PENDING_APPROVAL'
    APPROVED = 'APPROVED'
    DELIVERING = 'DELIVERING'
    DELIVERED = 'DELIVERED'
    FAILED = 'FAILED'
    REJECTED = 'REJECTED'
    EXPIRED = 'EXPIRED'
    CONSUMED = 'CONSUMED'
    ACK_TIMEOUT = 'ACK_TIMEOUT'


class Event(StrEnum):
    ENQUEUE = 'enqueue'
    HUMAN_APPROVE = 'human_approve'
    HUMAN_REJECT = 'human_reject'
    TIMEOUT = 'timeout'
    ROUTE = 'route'
    DELIVER_OK = 'deliver_ok'
    DELIVER_ERR = 'deliver_err'
    RETRY = 'retry'
    ESCALATE = 'escalate'
    # Known future events, intentionally absent from the Phase 1 map.
    AUTO_APPROVE = 'auto_approve'
    ACK_RECEIVED = 'ack_received'
    NO_ACK = 'no_ack'


class EnvelopeNotFound(ValueError):
    pass


class InvalidEvent(ValueError):
    pass


TRANSITIONS = MappingProxyType({
    (State.CAPTURED, Event.ENQUEUE): State.PENDING_APPROVAL,
    (State.PENDING_APPROVAL, Event.HUMAN_APPROVE): State.APPROVED,
    (State.PENDING_APPROVAL, Event.HUMAN_REJECT): State.REJECTED,
    (State.PENDING_APPROVAL, Event.TIMEOUT): State.EXPIRED,
    (State.APPROVED, Event.ROUTE): State.DELIVERING,
    (State.DELIVERING, Event.DELIVER_OK): State.DELIVERED,
    (State.DELIVERING, Event.DELIVER_ERR): State.FAILED,
    (State.FAILED, Event.RETRY): State.DELIVERING,
    (State.FAILED, Event.ESCALATE): State.PENDING_APPROVAL,
})

_REQUIRED = {
    Event.HUMAN_APPROVE: {'version_id'},
    Event.ROUTE: {'version_id', 'dest_path'},
    Event.RETRY: {'version_id', 'dest_path'},
    Event.DELIVER_OK: {'actual_sha256'},
    Event.TIMEOUT: {'reason'},
    Event.DELIVER_ERR: {'reason'},
    Event.ESCALATE: {'reason'},
}


def _validate(event, payload, actor):
    if not isinstance(actor, str) or not actor.strip():
        raise InvalidEvent('A nonempty actor from trusted caller context is required')
    if not isinstance(payload, dict):
        raise InvalidEvent(f'{event}: payload must be an object')
    required = _REQUIRED.get(event, set())
    optional = {'reason'} if event == Event.HUMAN_REJECT else set()
    if not required <= payload.keys() or payload.keys() - required - optional:
        raise InvalidEvent(f'{event}: missing or unexpected payload fields')
    for key, value in payload.items():
        if not isinstance(value, str) or not value.strip():
            raise InvalidEvent(f'{event}: {key} must be nonempty text')
    if event == Event.DELIVER_OK and not re.fullmatch('[0-9a-f]{64}', payload['actual_sha256']):
        raise InvalidEvent('deliver_ok: actual_sha256 must be a lowercase SHA-256 digest')


def transition(conn, envelope_id, event, *, expected_state, actor, payload):
    """Validate and apply exactly one transition and one audit in one transaction.

    Callers authenticate actor before invoking this service. Database lock errors
    propagate with the bounded timeout configured by store.connect().
    """
    with store.transaction(conn):
        envelope = store.get_envelope(conn, envelope_id)
        if envelope is None:
            raise EnvelopeNotFound(envelope_id)
        try:
            event = Event(event)
            expected_state = State(expected_state)
        except (ValueError, TypeError):
            raise InvalidEvent('Unknown event or expected state') from None
        _validate(event, payload, actor)
        target = TRANSITIONS.get((expected_state, event))
        if (target is None or envelope['state'] != expected_state or
                envelope['mode'] != 'manual' or envelope['trigger'] != 'human'):
            raise InvalidTransition(envelope_id, expected_state, target or event)

        if event == Event.HUMAN_APPROVE:
            store.set_approved(conn, envelope_id, payload['version_id'], actor,
                               expected_from=expected_state)
        elif event in (Event.ROUTE, Event.RETRY):
            if event == Event.RETRY:
                if payload['version_id'] != envelope['delivered_version_id']:
                    raise VersionMismatch(envelope_id, payload['version_id'])
                if payload['dest_path'] != envelope['delivered_path']:
                    raise InvalidEvent('retry must preserve the recorded destination')
            store.record_delivery_intent(
                conn, envelope_id, payload['version_id'], payload['dest_path'],
                expected_from=expected_state, actor=actor, event=event)
        elif event == Event.DELIVER_OK:
            store.record_delivered(conn, envelope_id, payload['actual_sha256'],
                                   expected_from=expected_state, actor=actor)
        elif event == Event.ESCALATE:
            store.reset_for_reapproval(conn, envelope_id, actor=actor, reason=payload['reason'])
        else:
            store.set_state(conn, envelope_id, expected_state, target, event, actor, payload)
        return store.get_envelope(conn, envelope_id)
