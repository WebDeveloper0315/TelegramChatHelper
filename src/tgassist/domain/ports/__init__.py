"""Domain ports: every interface the application depends on.

Infrastructure supplies the adapters. Declaring ports here -- rather than beside
their implementations -- is what allows the domain to be exercised without a
database, a network or a model. See ``docs/API.md``.
"""

from tgassist.domain.ports.clock import Clock
from tgassist.domain.ports.event_bus import EventBus, EventHandler, Subscription
from tgassist.domain.ports.id_generator import IdGenerator
from tgassist.domain.ports.secret_store import SecretStore

__all__ = [
    "Clock",
    "EventBus",
    "EventHandler",
    "IdGenerator",
    "SecretStore",
    "Subscription",
]
