#!/usr/bin/env python3
"""Run the reference Xerrameca Runner receiver.

Required environment:
  XERRAMECA_RUNNER_SECRET
  PLURIBUS_API_KEY
Optional:
  PLURIBUS_URL=http://127.0.0.1:8000
  XERRAMECA_HANDLER=my_agent.xerrameca:handle_turn
  XERRAMECA_RECEIVER_DB=./xerrameca_receiver.db
  XERRAMECA_RECEIVER_HOST=0.0.0.0
  XERRAMECA_RECEIVER_PORT=8090
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn


if __name__ == "__main__":
    uvicorn.run(
        "pluribus.xerrameca.receiver:app",
        host=os.getenv("XERRAMECA_RECEIVER_HOST", "0.0.0.0"),
        port=int(os.getenv("XERRAMECA_RECEIVER_PORT", "8090")),
        workers=1,
    )
