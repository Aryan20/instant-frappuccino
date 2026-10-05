"""Platform-neutral domain logic.

Nothing in this package may import Qt: it must stay usable from tests, a CLI, or a
different UI toolkit. OS differences (paths, PATH lookup, process env) are isolated in
``paths`` and ``env``.
"""
