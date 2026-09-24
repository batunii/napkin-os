"""The Napkin middleware: `napkin.middleware/1` (docs/contracts/middleware-api.md).

The middleware holds the logic — stage orchestration, the identify rules,
tiering, derived confidence, the per-market merge, contests, the cite rule —
and talks to three components through ports: the model (Anthropic Messages
API), research (an HTTP search/discovery service) and the knowledge layers
(a Python protocol). Which implementation sits behind each port is
configuration only.
"""

API = "napkin.middleware/1"
BACKEND = "napkin-middleware/0.1"
