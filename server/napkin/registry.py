"""The handler registry (W2-C1, decisions M2 / M4).

Explicit registration, in this one file: `name -> {major: module}`. No
decorators, no import side effects, no discovery. A pipeline names
`handler: name@major`; resolution is exact — an unknown name, a handler
registered for another task, or an unimplemented major is a hard error, never
a fall-through. Every installed pipeline is resolved at startup, so a typo
fails the deploy, not the first request.
"""

from __future__ import annotations

import re
from pathlib import Path

from .capabilities import CAPABILITY_VERSION
from .handlers import (answer_question, compose_report, extract_ask, research_lens, start_campaign,
                       synthesise_findings)
from .util import bad

REGISTRY = {
    "extract_ask": {1: extract_ask},
    "research_lens": {1: research_lens},
    "synthesise_findings": {1: synthesise_findings},
    "start_campaign": {1: start_campaign},
    "answer_question": {1: answer_question},
    "compose_report": {1: compose_report},
}
# Used only when a document carries no pipeline: the declared built-in map.
BUILTIN_PIPELINE = {
    "extract_ask": "extract_ask@1", "research_lens": "research_lens@1",
    "synthesise_findings": "synthesise_findings@1", "start_campaign": "start_campaign@1",
    "answer_question": "answer_question@1", "compose_report": "compose_report@1",
}
TASKS = set(BUILTIN_PIPELINE) | {"job_status"}


class RegistryError(Exception):
    pass


def lookup(declared: str, task: str):
    """'name@major' for `task` -> (module, 'name@major.minor'). Raises 400 unknown_handler."""
    m = re.fullmatch(r"([a-z_]+)@(\d+)(?:\.\d+)*", str(declared or ""))
    if not m:
        raise bad(f"handler '{declared}' for task '{task}' is not name@major", "unknown_handler")
    name, major = m.group(1), int(m.group(2))
    reg = REGISTRY.get(name)
    if reg is None:
        raise bad(f"no handler named '{name}' is registered (registered: {', '.join(sorted(REGISTRY))})",
                  "unknown_handler")
    mod = reg.get(major)
    if mod is None:
        raise bad(f"handler '{name}' has no major version {major} (registered: {sorted(reg)})", "unknown_handler")
    if mod.TASK != task:
        raise bad(f"handler '{name}' implements task '{mod.TASK}', not '{task}'", "unknown_handler")
    return mod, f"{mod.NAME}@{mod.VERSION}"


def resolve(task: str, clan: dict):
    """The document's pipeline decides; no pipeline -> the built-in map."""
    pipeline = clan.get("pipeline")
    if pipeline:
        if not isinstance(pipeline, dict) or not isinstance(pipeline.get("tasks"), dict):
            raise bad("clan.pipeline is present but has no tasks map")
        decl = pipeline["tasks"].get(task)
        if decl is None:
            raise bad(f"task '{task}' is not declared by this document's pipeline", "unknown_task")
        declared = decl.get("handler") if isinstance(decl, dict) else decl
    else:
        declared = BUILTIN_PIPELINE[task]
    return lookup(declared, task)


def parse_pipeline(text: str) -> dict:
    """task -> declared handler, from a pipeline.yaml (no YAML dependency)."""
    out, in_tasks, cur = {}, False, None
    for line in text.splitlines():
        if re.match(r"^tasks:\s*$", line):
            in_tasks = True
            continue
        if in_tasks and re.match(r"^\S", line):
            break
        m = re.match(r"^  ([a-z_]+):\s*$", line)
        if in_tasks and m:
            cur = m.group(1)
        m = re.match(r"^    handler:\s*(\S+)\s*$", line)
        if in_tasks and cur and m:
            out[cur] = m.group(1)
    return out


def check_modules():
    for name, majors in REGISTRY.items():
        for major, mod in majors.items():
            if mod.NAME != name or int(mod.VERSION.split(".")[0]) != major:
                raise RegistryError(f"{mod.__name__} is registered as {name}@{major} but declares "
                                    f"{mod.NAME}@{mod.VERSION}")
            if mod.CAPABILITY_MAJOR != CAPABILITY_VERSION:
                raise RegistryError(f"{name}@{mod.VERSION} needs capability v{mod.CAPABILITY_MAJOR}; the middleware "
                                    f"provides v{CAPABILITY_VERSION}")


def resolve_at_startup(pipeline_files: list[str]) -> dict:
    """Resolve every handler every installed pipeline names; raise on the first
    that does not resolve, naming what is available."""
    check_modules()
    resolved = {}
    for path in pipeline_files:
        decls = parse_pipeline(Path(path).read_text())
        for task, declared in decls.items():
            if task not in TASKS:
                raise RegistryError(f"{path}: task '{task}' is not in napkin.middleware/1")
            try:
                resolved[(path, task)] = lookup(declared, task)[1]
            except Exception as e:
                raise RegistryError(f"{path}: {getattr(e, 'message', e)}") from None
    return resolved
