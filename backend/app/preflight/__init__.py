"""Diagnóstico de integraciones de solo lectura (go-live). Ver app/preflight/core.py y docs/data-model.md §20."""

from app.preflight.core import AREA_LABELS, AREAS, Context, Result, exit_code, run_and_save

__all__ = ["AREAS", "AREA_LABELS", "Context", "Result", "exit_code", "run_and_save"]
