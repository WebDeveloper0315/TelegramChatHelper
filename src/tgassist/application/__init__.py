"""Application layer: use cases, policies, event handlers and the composition root.

Depends only on the domain. The single exception is ``container``, the composition
root, which is permitted to construct infrastructure adapters (ADR-011).
"""
