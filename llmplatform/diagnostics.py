from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any


def exception_diagnostics(exc: BaseException, frame_limit: int = 12) -> dict[str, Any]:
    """Describe failure locations without serializing locals or source text."""
    frames = traceback.extract_tb(exc.__traceback__)[-frame_limit:]
    causes: list[str] = []
    cause = exc.__cause__ or exc.__context__
    seen = {id(exc)}
    while cause is not None and id(cause) not in seen and len(causes) < 5:
        seen.add(id(cause))
        causes.append(type(cause).__name__)
        cause = cause.__cause__ or cause.__context__
    return {
        "exception_type": type(exc).__name__,
        "location": (
            {
                "module": Path(frames[-1].filename).name,
                "function": frames[-1].name,
                "line": frames[-1].lineno,
            }
            if frames
            else None
        ),
        "stack": [
            {
                "module": Path(frame.filename).name,
                "function": frame.name,
                "line": frame.lineno,
            }
            for frame in frames
        ],
        "causes": causes,
    }
