"""SimConnector: pairs a codec with a TCP transport, and the codec registry.

A connector is the unit the server fans shots out to. Adding a new simulator
means writing a codec and registering it here — nothing else in the server or
transport changes.
"""

import logging
from typing import Callable, List, Optional

from openflight.sim.config import ConnectorConfig
from openflight.sim.transport import DEFAULT_BACKOFF, Codec, TcpSimClient
from openflight.sim.types import InboundEvent, ResolvedShot, StatusEvent

logger = logging.getLogger(__name__)


class SimConnector:
    """One simulator endpoint: codec + transport + per-target callback routing.

    Callbacks are invoked as ``on_status(target, StatusEvent)`` and
    ``on_inbound(target, InboundEvent)`` so the server can multiplex several
    connectors through a single pair of handlers.
    """

    def __init__(
        self,
        codec: Codec,
        host: str,
        port: int,
        heartbeat_interval_s: float = 5.0,
        on_status: Optional[Callable[[str, StatusEvent], None]] = None,
        on_inbound: Optional[Callable[[str, InboundEvent], None]] = None,
        backoff_seconds=DEFAULT_BACKOFF,
    ):
        self.codec = codec
        self.name = codec.name
        self.host = host
        self.port = port
        self._on_status_user = on_status
        self._on_inbound_user = on_inbound
        self._client = TcpSimClient(
            host=host,
            port=port,
            codec=codec,
            heartbeat_interval_s=heartbeat_interval_s,
            name=codec.name,
            on_inbound=self._handle_inbound,
            on_status=self._handle_status,
            backoff_seconds=backoff_seconds,
        )

    def _handle_status(self, event: StatusEvent) -> None:
        if self._on_status_user is not None:
            self._on_status_user(self.name, event)

    def _handle_inbound(self, event: InboundEvent) -> None:
        if self._on_inbound_user is not None:
            self._on_inbound_user(self.name, event)

    def start(self) -> None:
        self._client.start()

    def stop(self) -> None:
        self._client.stop()

    def is_connected(self) -> bool:
        return self._client.is_connected()

    @property
    def state(self):
        return self._client.state

    @property
    def peer(self) -> str:
        """The simulator's own name if it reported one (e.g. "Open-Birdie"), else ""."""
        return self._client.peer

    def _shot_bytes(self, resolved: ResolvedShot) -> bytes:
        # Built for the simulator last seen on this link: it names itself on
        # every connection, and a shot held through a dropped link is almost
        # always delivered to the same one.
        return self.codec.build_shot(resolved, peer=self.peer)

    def send_shot(self, resolved: ResolvedShot) -> None:
        """Serialize and send a resolved shot. Raises OSError if the socket fails."""
        self._client.send_raw(self._shot_bytes(resolved))

    def deliver_shot(self, resolved: ResolvedShot, on_delivered=None) -> bool:
        """Send a shot, or hold it through a dropped link or network switch.

        Never raises. True when it went out now; False when it is waiting for
        the connection (it is sent, and ``on_delivered`` called, once it does).
        """
        return self._client.deliver(self._shot_bytes(resolved), on_delivered=on_delivered)


def _codec_for(cfg: "ConnectorConfig") -> Codec:
    """Instantiate the codec for a connector type. Import is local to avoid an
    import cycle (the codec imports sim.types/resolver).

    The OpenConnect V1 codec is shared: GSPro uses it on 921; OpenGolfSim uses it
    on its Developer API (3111, which speaks OpenConnect), named "opengolfsim" so
    the UI/logs/config say OpenGolfSim rather than GSPro; PAR-TEE listens for it
    on the phone's Wi-Fi address (921), named "partee" for the same reason.
    """
    if cfg.type not in ("gspro", "opengolfsim", "partee"):
        raise ValueError(f"unknown simulator connector type: {cfg.type!r}")
    from openflight.gspro.codec import GSProCodec  # pylint: disable=import-outside-toplevel

    return GSProCodec(device_id=cfg.device_id, units=cfg.units, name=cfg.type)


def build_connector(
    cfg: "ConnectorConfig",
    on_status: Optional[Callable[[str, StatusEvent], None]] = None,
    on_inbound: Optional[Callable[[str, InboundEvent], None]] = None,
    backoff_seconds=DEFAULT_BACKOFF,
) -> SimConnector:
    """Build a single connector from a resolved ConnectorConfig."""
    codec = _codec_for(cfg)
    return SimConnector(
        codec=codec,
        host=cfg.host,
        port=cfg.port,
        heartbeat_interval_s=cfg.heartbeat_interval_s,
        on_status=on_status,
        on_inbound=on_inbound,
        backoff_seconds=backoff_seconds,
    )


def build_connectors(
    cfgs: List["ConnectorConfig"],
    on_status: Optional[Callable[[str, StatusEvent], None]] = None,
    on_inbound: Optional[Callable[[str, InboundEvent], None]] = None,
    backoff_seconds=DEFAULT_BACKOFF,
) -> List[SimConnector]:
    """Build every connector in a resolved config list."""
    return [
        build_connector(
            cfg, on_status=on_status, on_inbound=on_inbound, backoff_seconds=backoff_seconds
        )
        for cfg in cfgs
    ]
