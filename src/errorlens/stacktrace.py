"""Exceptions and stack frames → event data."""

from __future__ import annotations

import linecache
import os
import sys
import sysconfig
from types import FrameType, TracebackType
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Type

from .serialize import safe_repr, truncate

ExcInfo = Tuple[Optional[Type[BaseException]], Optional[BaseException], Optional[TracebackType]]

CONTEXT_LINES = 5
MAX_FRAMES = 250  # keep the outermost and innermost frames beyond this
MAX_VARS = 40
MAX_CHAIN = 10


def _library_roots() -> Tuple[str, ...]:
    roots = set()
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        try:
            p = sysconfig.get_paths().get(key)
        except Exception:  # noqa: BLE001
            p = None
        if p:
            roots.add(os.path.realpath(p))
    return tuple(roots)


_LIBRARY_ROOTS = _library_roots()
_LIBRARY_MARKERS = (os.sep + "site-packages" + os.sep, os.sep + "dist-packages" + os.sep)


class FrameRules:
    """Decides which frames are the user's own code (in_app)."""

    def __init__(self, include: Sequence[str] = (), exclude: Sequence[str] = (), project_root: Optional[str] = None):
        self.include = tuple(include)
        self.exclude = tuple(exclude)
        self.project_root = os.path.realpath(project_root) if project_root else None

    @staticmethod
    def _matches(module: str, prefixes: Iterable[str]) -> bool:
        return any(module == p or module.startswith(p + ".") for p in prefixes)

    def in_app(self, module: str, abs_path: str) -> bool:
        if module and self._matches(module, self.include):
            return True
        if module and self._matches(module, self.exclude):
            return False
        if module.startswith("errorlens.") or module == "errorlens":
            return False
        if not abs_path or abs_path.startswith("<"):
            return False
        if any(m in abs_path for m in _LIBRARY_MARKERS):
            return False
        real = os.path.realpath(abs_path)
        if self.project_root:
            return real.startswith(self.project_root + os.sep)
        return not any(real.startswith(root + os.sep) for root in _LIBRARY_ROOTS)


def _short_filename(module: str, abs_path: str) -> str:
    """shop/pricing.py rather than /srv/app/venv/.../shop/pricing.py."""
    if not abs_path:
        return "<unknown>"
    if module and module != "__main__":
        parts = module.split(".")
        base = os.path.basename(abs_path)
        if base == "__init__.py":
            parts.append("__init__")
        tail = os.sep.join(parts[:-1] + [base]) if parts else base
        if abs_path.endswith(tail):
            return tail.replace(os.sep, "/")
    return os.path.basename(abs_path)


def _context(abs_path: str, lineno: int, module_globals: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not abs_path or lineno < 1:
        return {}
    try:
        lines = linecache.getlines(abs_path, module_globals)
    except Exception:  # noqa: BLE001
        return {}
    if not lines or lineno > len(lines):
        return {}
    i = lineno - 1

    def clean(line: str) -> str:
        return truncate(line.rstrip("\r\n"), 300)

    return {
        "pre_context": [clean(l) for l in lines[max(0, i - CONTEXT_LINES):i]],
        "context_line": clean(lines[i]),
        "post_context": [clean(l) for l in lines[i + 1:i + 1 + CONTEXT_LINES]],
    }


def _frame_vars(frame: FrameType) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        items = list(frame.f_locals.items())
    except Exception:  # noqa: BLE001
        return out
    for name, value in items[:MAX_VARS]:
        if name.startswith("__") and name.endswith("__"):
            continue
        out[name] = safe_repr(value)
    return out


def frame_to_dict(frame: FrameType, lineno: int, rules: FrameRules, with_locals: bool, with_source: bool) -> Dict[str, Any]:
    code = frame.f_code
    abs_path = code.co_filename or ""
    module = str(frame.f_globals.get("__name__") or "")
    function = getattr(code, "co_qualname", None) or code.co_name
    in_app = rules.in_app(module, abs_path)
    d: Dict[str, Any] = {
        "filename": _short_filename(module, abs_path),
        "abs_path": abs_path,
        "module": module,
        "function": function,
        "lineno": lineno,
        "in_app": in_app,
    }
    if with_source:
        d.update(_context(abs_path, lineno, frame.f_globals))
    if with_locals and in_app:
        d["vars"] = _frame_vars(frame)
    return d


def _trim(frames: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if len(frames) <= MAX_FRAMES:
        return frames
    keep = MAX_FRAMES // 2
    return frames[:keep] + frames[-keep:]


def frames_from_traceback(tb: Optional[TracebackType], rules: FrameRules, with_locals: bool, with_source: bool) -> List[Dict[str, Any]]:
    frames = []
    while tb is not None:
        if not tb.tb_frame.f_locals.get("__traceback_hide__"):
            frames.append(frame_to_dict(tb.tb_frame, tb.tb_lineno, rules, with_locals, with_source))
        tb = tb.tb_next
    return _trim(frames)  # oldest first


def current_stack(rules: FrameRules, with_source: bool, skip: int = 0) -> List[Dict[str, Any]]:
    """The calling stack (for messages with attach_stacktrace)."""
    frames = []
    f: Optional[FrameType] = sys._getframe(skip + 1)
    while f is not None:
        module = str(f.f_globals.get("__name__") or "")
        if not module.startswith("errorlens"):
            frames.append(frame_to_dict(f, f.f_lineno, rules, False, with_source))
        f = f.f_back
    frames.reverse()
    return _trim(frames)


def _type_name(exc_type: type) -> str:
    return getattr(exc_type, "__qualname__", None) or exc_type.__name__


def exception_chain(exc: BaseException) -> List[BaseException]:
    """The exception and its causes, oldest first (as in a printed traceback)."""
    chain: List[BaseException] = []
    seen = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen and len(chain) < MAX_CHAIN:
        seen.add(id(cur))
        chain.append(cur)
        if cur.__cause__ is not None:
            cur = cur.__cause__
        elif cur.__context__ is not None and not cur.__suppress_context__:
            cur = cur.__context__
        else:
            cur = None
    chain.reverse()
    return chain


def exceptions_from(exc: BaseException, rules: FrameRules, with_locals: bool, with_source: bool,
                    mechanism: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    values = []
    chain = exception_chain(exc)
    for i, e in enumerate(chain):
        t = type(e)
        try:
            value = str(e)
        except Exception:  # noqa: BLE001
            value = "<exception str() failed>"
        item: Dict[str, Any] = {
            "type": _type_name(t),
            "module": t.__module__ if t.__module__ not in ("builtins", "__builtin__") else None,
            "value": truncate(value, 8192),
            "stacktrace": {"frames": frames_from_traceback(e.__traceback__, rules, with_locals, with_source)},
        }
        if i == len(chain) - 1:
            item["mechanism"] = mechanism or {"type": "generic", "handled": True}
        else:
            item["mechanism"] = {"type": "chained", "handled": True,
                                 "source": "__cause__" if chain[i + 1].__cause__ is e else "__context__"}
        notes = getattr(e, "__notes__", None)
        if notes:
            item["value"] = truncate(item["value"] + "\n" + "\n".join(map(str, notes)), 8192)
        if item["module"] is None:
            del item["module"]
        values.append(item)
    return values
