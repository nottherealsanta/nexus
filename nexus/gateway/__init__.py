"""Opt-in remote gateway: authenticated native devices in front of workspace daemons.

The gateway is a separate loopback process (``nexus gateway serve``), not a
daemon transport. It never reduces events, opens sessions on its own or reads
session files: every byte it serves comes from a workspace daemon's existing
browser surface, reached through the daemon's Unix socket (``WebLaunch``) and
then loopback HTTP with a ticket-derived cookie. See ``docs/gateway.md``.
"""
