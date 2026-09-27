"""Backward-compatible import path for the transport-neutral host client."""

from ...client.protocol import (
    Client,
    ClientError,
    FacadeError,
    ProtocolVersionError,
    Transport,
    TransportClosed,
)

__all__ = [
    "Client",
    "ClientError",
    "FacadeError",
    "ProtocolVersionError",
    "Transport",
    "TransportClosed",
]
