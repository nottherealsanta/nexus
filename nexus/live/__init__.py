"""GPT Live voice calls: a daemon-side manager for realtime spoken conversations.

See ``docs/live.md`` and ``plans/done/GPT_LIVE_PLAN.md``. The package sits
beside ``nexus/voice`` as a manager: it imports only ``nexus.config``,
``nexus.errors``, ``nexus.events`` and ``nexus.util`` plus the protocols it
declares itself; the host layer supplies the session seam (``LiveHost``).
"""
