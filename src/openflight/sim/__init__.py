"""Protocol-agnostic simulator-connector framework.

A *codec* owns one simulator's wire format; the shared transport, resolver, and
types are sim-neutral. Adding a simulator = one new codec + a registry entry in
``sim.codec``. See docs/simulator/ for the full pattern.
"""

from .codec import SimConnector, build_connector, build_connectors
from .config import ConnectorConfig, load_sim_config
from .resolver import resolve_shot
from .types import (
    SHOT_NUMBER_MAX,
    ConnectionState,
    InboundEvent,
    IncompleteShotError,
    PeerInfo,
    PlayerState,
    PlayerUpdate,
    ResolvedShot,
    ShotAck,
    SimError,
    StatusEvent,
    initial_shot_counter,
)

__all__ = [
    "ConnectionState",
    "ConnectorConfig",
    "IncompleteShotError",
    "InboundEvent",
    "PeerInfo",
    "PlayerState",
    "PlayerUpdate",
    "ResolvedShot",
    "SHOT_NUMBER_MAX",
    "ShotAck",
    "SimConnector",
    "SimError",
    "StatusEvent",
    "build_connector",
    "build_connectors",
    "initial_shot_counter",
    "load_sim_config",
    "resolve_shot",
]
