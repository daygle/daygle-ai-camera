"""FastAPI APIRouter package.

All routers use direct imports from canonical submodules (``app.state``,
``app.deps``, ``app.auth_gates``, ``app.config_facades``, etc.) rather than
the former ``import app.main as main`` hybrid pattern. Shared mutable state
lives in ``app.state``; dependency injection lives in ``app.deps``.
"""
