#!/usr/bin/env python3
"""Validate the platform's configured Gemini/Vertex authentication."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.services.google_auth import auth_summary, probe_vertex_or_api_key  # noqa: E402

print("Safe configuration:")
print(json.dumps(auth_summary().safe_dict(), indent=2))
print("\nCalling configured Gemini model...")
print(json.dumps(probe_vertex_or_api_key(), indent=2))
