"""Domain layer: entities, value objects, ports, pure services, events and errors.

This package is the centre of the architecture. It declares the contracts the rest
of the system implements and must never import from the application, infrastructure
or presentation layers, nor from any third-party package (ADR-011).

The rule is enforced by ``.importlinter`` contracts and by
``tests/architecture/test_layers.py``.
"""
