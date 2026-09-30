"""
brief_llm.py — how the brief engine talks to models (split out of parse_brief.py, 2026-09-27).

Everything between a pipeline stage and a model's reply lives here:

  providers and the chain   PROVIDERS, resolve_provider, model_for, _model_chain (Claude-only
                            by default; BRIEF_ALLOW_NONCLAUDE=1 allows other links), cooldowns
  model routes              ROUTES / ROUTE_EFFORT / route_models: each call names its job and
                            the job picks [model, fallback]; a judge never runs on its writer
                            (ADR 0011)
  transports                _chat_anthropic (API key), _chat_claude_cli (the Claude Code
                            login, `claude -p`), _chat_openai_compatible (NIM, Groq, Cerebras,
                            Gemini, OpenAI, Ollama); BRIEF_CLAUDE_TRANSPORT picks api | cli | auto
  calls                     _call_link (one link), _json_call (a chain walk that must return
                            JSON: retries, truncation, refusals, accept checks), _chat (text)
  JSON readers              _loads_lenient (whole-object or salvaging)
  the call ledger           _stats_scope / _scoped / _stats_*: per-run, per-thread counts of
                            calls, tokens, cache tokens and the model that answered
  errors                    NoClaudeAvailable and the internal _RateLimited, _Truncated,
                            _Refused, _NoEvidence

parse_brief re-exports every name here, so `parse_brief._json_call` and friends keep
working, and assigning one on parse_brief (as the tests' monkeypatch does) also assigns it
here (see parse_brief._ForwardingModule). New code should import from this module.
"""
from __future__ import annotations

import functools
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request

# The system prompt a call gets when its caller passes none. parse_brief sets it to its
# extraction prompt on import, which is what every such call used before the split.
DEFAULT_SYSTEM = "You are a precise assistant. Follow the instructions exactly; output only what is asked."


# A browser-like UA so Cloudflare-fronted APIs (Groq, Cerebras) don't 1010-block us.
_HTTP_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# ---------------------------------------------------------------------------
# LLM call ledger — instrumentation only, zero behavioural effect. Reset per
# run(); snapshot lands in brief_object.json → meta.llm_stats so optimisation
# work is measured, not eyeballed. prompt/completion tokens come from provider
# `usage` when present; chars are always counted as the fallback ruler.
# ---------------------------------------------------------------------------
_LLM_STATS = {}
_STATS_LOCK = __import__("threading").Lock()   # loops 3–7 retrieval/rerank run in threads
# A run's ledger travels with the thread (and, via _scoped, with the threads it starts), so
# two briefs in one process — or a name-derivation call next to a running brief on the
# agent-server — never write into each other's numbers (audit 2026-09-24, critic-G11).
_STATS_TL = threading.local()

_EMPTY_STATS = {"calls": 0, "http_attempts": 0, "input_chars": 0, "output_chars": 0,
                "prompt_tokens": 0, "completion_tokens": 0, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "retries": 0, "rate_limited": 0, "truncations": 0,
                "refusals": 0, "by_provider": {}, "answered_by": {}}


def _ledger() -> dict:
    """The ledger this thread writes to: the run-scoped one set by _stats_scope (and carried
    into worker threads by _scoped), else the module-global _LLM_STATS."""
    led = getattr(_STATS_TL, "stats", None)
    if led is not None:
        return led
    if not _LLM_STATS:
        _stats_reset()
    return _LLM_STATS


class _stats_scope:
    """`with _stats_scope() as ledger:` gives the calling thread a fresh, private ledger for
    the block; nested calls in this thread (and threads started through _scoped) record into
    it. run() uses it so meta.llm_stats is exactly that brief's calls."""

    def __enter__(self):
        """Install a fresh ledger for this thread; return it."""
        self._prev = getattr(_STATS_TL, "stats", None)
        self.ledger = {**_EMPTY_STATS, "by_provider": {}, "answered_by": {}}
        _STATS_TL.stats = self.ledger
        return self.ledger

    def __exit__(self, *exc):
        """Restore whatever ledger the thread had before."""
        _STATS_TL.stats = self._prev
        return False


def _scoped(fn):
    """Wrap `fn` so that, run on another thread (a pool submit/map), it records into the
    ledger of the thread that called _scoped. Every pool inside a run uses it."""
    led = getattr(_STATS_TL, "stats", None)

    def wrapped(*a, **k):
        """Run fn with the parent thread's ledger installed."""
        _STATS_TL.stats = led
        return fn(*a, **k)
    return wrapped


def _stats_reset():
    """Zero the LLM call ledger (_LLM_STATS) under the stats lock, by_provider
    included. Direct calls from tests and scripts land here; run() records into its own
    scoped ledger (see _stats_scope)."""
    with _STATS_LOCK:
        _LLM_STATS.clear()
        _LLM_STATS.update({**_EMPTY_STATS, "by_provider": {}, "answered_by": {}})


def _stats_call(provider_label: str, in_chars: int):
    """Count one outbound request to a link in the LLM call ledger: bumps `calls`,
    adds `in_chars` (system + user prompt length) to `input_chars` and tallies the request
    under by_provider[provider_label]. Instrumentation only."""
    led = _ledger()
    with _STATS_LOCK:
        led["calls"] += 1
        led["input_chars"] += in_chars
        bp = led["by_provider"]
        bp[provider_label] = bp.get(provider_label, 0) + 1


def _stats_usage(usage: dict | None, out_chars: int):
    """Add one reply to the LLM call ledger: `out_chars` to `output_chars`, and the
    provider's usage when a usage dict is given: prompt_tokens (uncached input),
    completion_tokens, and the separately priced cache_read_tokens / cache_creation_tokens
    (missing or None counts as 0). Instrumentation only."""
    led = _ledger()
    with _STATS_LOCK:
        led["output_chars"] += out_chars
        if usage:
            for k in ("prompt_tokens", "completion_tokens", "cache_read_tokens", "cache_creation_tokens"):
                led[k] = led.get(k, 0) + int(usage.get(k) or 0)


def _stats_answered(provider_label: str):
    """Record that `provider_label` is the link whose reply was USED (parsed and accepted),
    as opposed to merely attempted (by_provider). meta.extraction_mode and the synthesis
    label are read from this, so a brief is labelled with the model that wrote it."""
    led = _ledger()
    with _STATS_LOCK:
        ab = led.setdefault("answered_by", {})
        ab[provider_label] = ab.get(provider_label, 0) + 1


def _stats_bump(key: str):
    """Add one to a counter in the ledger (truncations, refusals, ...)."""
    led = _ledger()
    with _STATS_LOCK:
        led[key] = led.get(key, 0) + 1


def _stats_snapshot() -> dict:
    """A deep copy of the current ledger. The old shallow copy shared by_provider with the
    live ledger, so the critic call made after run() changed a finished brief's numbers."""
    import copy
    with _STATS_LOCK:
        return copy.deepcopy(_ledger())


# provider -> (default base_url, api-key env var, default model)
# nim = NVIDIA NIM (Nemotron) via build.nvidia.com — free for prototyping on Inception.
PROVIDERS = {
    "nim":    ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY",
               "nvidia/llama-3.1-nemotron-70b-instruct"),
    # Free-tier, OpenAI-compatible alternatives — faster/steadier than NIM for this
    # workload. Get a free key, set BRIEF_PROVIDER + the key env var, done.
    "groq":     ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "llama-3.3-70b-versatile"),
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", "llama-3.3-70b"),
    "gemini":   ("https://generativelanguage.googleapis.com/v1beta/openai",
                 "GEMINI_API_KEY", "gemini-2.0-flash"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-4o"),
    "ollama": ("http://localhost:11434/v1", None, "llama3.1"),
}


def resolve_provider() -> str:
    """Explicit BRIEF_PROVIDER wins; otherwise auto-detect from whichever key is set."""
    p = os.environ.get("BRIEF_PROVIDER", "").lower().strip()
    if p:
        return p
    for env, prov in (("GROQ_API_KEY", "groq"), ("CEREBRAS_API_KEY", "cerebras"),
                      ("GEMINI_API_KEY", "gemini"), ("NVIDIA_API_KEY", "nim"),
                      ("ANTHROPIC_API_KEY", "anthropic"), ("OPENAI_API_KEY", "openai")):
        if os.environ.get(env):
            return prov
    return ""


def model_for(provider: str) -> str:
    """Return the model id for `provider`: BRIEF_MODEL when set, 'claude-opus-4-6' for
    anthropic, else the provider's default in PROVIDERS ('?' for an unknown provider).
    Used for the run's extraction_mode label and as the default model of the Anthropic path,
    the loop synthesis and a pinned provider's lead link."""
    if os.environ.get("BRIEF_MODEL"):
        return os.environ["BRIEF_MODEL"]
    if provider == "anthropic":
        return "claude-opus-4-6"
    return PROVIDERS.get(provider, (None, None, "?"))[2]


class _RateLimited(RuntimeError):
    """A link returned HTTP 429. Raised fast (no backoff) so the chain fails over and the
    link is put on a short cooldown — see _LINK_COOLDOWN."""


class _Truncated(RuntimeError):
    """The reply hit the output cap (stop_reason max_tokens / finish_reason length). Raised
    instead of returning the partial text, so _json_call retries once with more room and
    never parses half a reply — a tournament cut off mid-list used to become one draft
    (audit F8)."""


class _NoEvidence(RuntimeError):
    """Retrieval ran but returned no evidence at all: loops_3_7 falls back to the digests
    with that reason recorded instead of reporting an enabled, empty RAG run (audit RAG-2)."""


class NoClaudeAvailable(RuntimeError):
    """The chain is Claude-only and no route to Claude exists (no API key for transport
    api, no `claude` CLI for transport cli). Raised by run() before any call, so a brief is
    never quietly written by another model (Sai, 2026-09-25), and at the end of a run in
    which not one Claude call answered, so a heuristic brief is never returned as normal."""


class _Refused(RuntimeError):
    """The model declined to answer (stop_reason refusal). Logged loudly and counted; the
    chain moves on to its next link, which by default is another Claude model or nothing
    (audit BW3: a refusal used to be handed silently to a non-Claude link)."""


# Right-sized output budgets per call class. max_tokens counts against free-tier
# TPM budgets (Groq bills the CAP, not actual output), so a global 4000 was ~60%
# waste — judges return ~100-token verdicts. (BRIEF_MAX_TOKENS, which overrode every cap on
# the OpenAI-compatible links, was removed 2026-09-29, audit C9.)
MAXTOK_JUDGE = 500       # rubric gates / judges / territory gate / rerank: tiny JSON verdicts
MAXTOK_GEN = 1200        # candidate generation / refine: one field's worth of text
MAXTOK_SYNTH_ONE = 700    # one loop's synthesis paragraph (was 2500 for all five in one call)
MAXTOK_EXTRACT = 8000    # extraction / scorecard / golden: one big JSON. Was 2500: measured 2026-09-23 on a
                         # real 4,156-char client brief, Loop-1 extraction hit stop_reason=max_tokens
                         # at 2,500 output tokens, the JSON was cut off, and every chain link failed the same way.
                         # A ceiling, not a cost: billing is per token actually produced.


# Per-request timeout for the OpenAI-compatible links (NIM, Groq, Cerebras, …). Was 300 s,
# retried 3x: on 2026-09-24 a fallback gpt-oss link timed out three times and held one
# brief for 941 s. 90 s still covers the slowest normal reply measured (~40 s).
LINK_TIMEOUT_S = float(os.environ.get("BRIEF_LINK_TIMEOUT", "90"))


def _chat_openai_compatible(base_url, key, model, user, provider_label="llm",
                            timeout=None, system=None, max_tokens=None, json_mode=False, schema=None):
    """One code path for NVIDIA NIM, OpenAI, and Ollama — all OpenAI-compatible."""
    timeout = timeout or LINK_TIMEOUT_S
    # NOTE: do NOT prepend a "detailed thinking off" system message for
    # Nemotron — on NIM a second system message displaces the real one and
    # the model ignores the JSON instruction entirely (verified 2026-06-10).
    messages = [{"role": "system", "content": system or DEFAULT_SYSTEM},
                {"role": "user", "content": user}]
    payload = {
        "model": model, "temperature": 0.2,
        "max_tokens": int(max_tokens or MAXTOK_EXTRACT),
        "messages": messages,
    }
    # Structured-output mode: cuts the unclean-JSON retries that multiply calls through
    # the chain. A 400 drops the flag and retries the same link (see the handler below),
    # so naming a provider that turns out not to accept it costs one round trip rather
    # than a chain hop — which is why `nim` is included despite being the backstop.
    if schema and json_mode:
        # A schema is a hard constraint, not a hint: the model cannot return prose.
        payload["response_format"] = {"type": "json_schema",
                                      "json_schema": {"name": "extraction", "strict": True,
                                                      "schema": schema}}
    elif json_mode and provider_label.split(":", 1)[0] in ("groq", "cerebras", "openai", "nim"):
        payload["response_format"] = {"type": "json_object"}
    # Disable thinking only for reasoning models — non-reasoning models (e.g.
    # llama-3.3-70b) don't support this flag and may error on it.
    if "reasoning" in model or "thinking" in model:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    body = json.dumps(payload).encode()
    # Groq/Cerebras sit behind Cloudflare, which blocks Python's default UA (error 1010).
    headers = {"Content-Type": "application/json", "User-Agent": _HTTP_UA}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions",
                                 data=body, headers=headers, method="POST")
    _stats_call(provider_label, len(system or DEFAULT_SYSTEM) + len(user))
    # Retry transient timeouts/network blips — these (not API errors) are what was
    # silently dropping briefs to heuristic mode on NIM.
    for attempt in range(3):
        led = _ledger()
        with _STATS_LOCK:
            led["http_attempts"] += 1
            if attempt:
                led["retries"] += 1
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
                choice = data["choices"][0]
                msg = choice["message"]
                content = msg.get("content") or ""
                # Reasoning models (e.g. nemotron-*-reasoning) emit <think>…</think>
                # before the answer. Strip it so _loads_lenient finds clean JSON.
                content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
                _stats_usage(data.get("usage"), len(content))
                if choice.get("finish_reason") == "length":
                    _stats_bump("truncations")
                    raise _Truncated(f"{provider_label}: reply hit the output cap")
                return content
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            # 429 = rate-limited: fail FAST so the chain fails over to a free sibling link
            # immediately, instead of burning ~15s backing off a provider that's saturated.
            if e.code == 429:
                _stats_bump("rate_limited")
                raise _RateLimited(f"HTTP 429 from {provider_label}") from None
            # A 400 right after adding response_format = this model rejects structured
            # output — drop the flag and retry the same link (don't burn a chain hop).
            if e.code == 400 and "response_format" in payload and attempt < 2:
                payload.pop("response_format", None)
                body = json.dumps(payload).encode()
                req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions",
                                             data=body, headers=headers, method="POST")
                continue
            # Transient server errors: one short backoff, then hand off to the chain.
            if e.code in (500, 502, 503) and attempt < 2:
                import time
                print(f"[i] {provider_label} HTTP {e.code} (attempt {attempt+1}/3), backing off…",
                      file=sys.stderr)
                time.sleep(2 * (attempt + 1))
                continue
            # Name the failure class. A dead link and an unentitled one print the same
            # "link failed" today, and they need opposite responses: one is a config fix
            # you own, the other is a vendor conversation. Both went unnoticed for four
            # weeks behind a working lead link because the log never said which.
            if e.code == 410:
                raise RuntimeError(
                    f"HTTP 410 from {provider_label}: model RETIRED by the provider — "
                    f"replace it in the chain. {detail}") from None
            if e.code == 404 and "not found for account" in detail.lower():
                raise RuntimeError(
                    f"HTTP 404 from {provider_label}: model exists in the catalog but is "
                    f"NOT ENTITLED to this account — a vendor/tier question, not a config "
                    f"one. {detail}") from None
            raise RuntimeError(f"HTTP {e.code} from {provider_label}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < 2:
                import time
                print(f"[i] {provider_label} timeout/blip (attempt {attempt+1}/3), retrying…",
                      file=sys.stderr)
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"{provider_label} unreachable after 3 attempts: {e}") from None


def list_models(provider="nim"):
    """Connectivity check: list available models (esp. Nemotron). For `--check`."""
    default_base, key_env, _ = PROVIDERS[provider]
    base_url = os.environ.get("BRIEF_BASE_URL", default_base)
    key = os.environ.get(key_env) if key_env else "ollama"
    req = urllib.request.Request(base_url.rstrip("/") + "/models",
                                 headers={"Authorization": f"Bearer {key}", "User-Agent": _HTTP_UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        ids = [m["id"] for m in json.loads(r.read()).get("data", [])]
    return ids


# Models that think before answering by default (measured 2026-09-24: Opus 5.5 spent ~900
# tokens thinking on a 220-token paragraph, even at effort low; Sonnet 5 ~150). Thinking
# counts against max_tokens, so the pipeline's tight per-call ceilings (700 for a synthesis
# paragraph) cut the answer off mid-JSON. They get this much extra room — a ceiling, billed
# only when used. Opus 4.6 does not think by default and gets none.
THINKING_HEADROOM = int(os.environ.get("BRIEF_THINKING_HEADROOM", "2500"))
_THINKING_MODELS = ("claude-opus-5", "claude-sonnet-5", "claude-fable", "claude-mythos")


def _thinking_headroom(model) -> int:
    """Extra output tokens for a model that thinks by default; 0 for one that does not."""
    return THINKING_HEADROOM if str(model or model_for("anthropic")).startswith(_THINKING_MODELS) else 0


_ANTHROPIC_CLIENTS: dict = {}
_ANTHROPIC_CLIENTS_LOCK = threading.Lock()


def _anthropic_client():
    """One Anthropic SDK client per (SDK class, API key), reused across calls: a client per
    call rebuilt the HTTP connection pool and TLS session every time (audit BW16). Keyed on
    the class too, so a test's fake SDK never meets a cached real client."""
    import anthropic
    key = os.environ["ANTHROPIC_API_KEY"]
    k = (anthropic.Anthropic, key)
    with _ANTHROPIC_CLIENTS_LOCK:
        if k not in _ANTHROPIC_CLIENTS:
            _ANTHROPIC_CLIENTS[k] = anthropic.Anthropic(api_key=key)
        return _ANTHROPIC_CLIENTS[k]


def _chat_anthropic(user, system=None, max_tokens=None, schema=None, model=None):
    """The Anthropic link. `schema` turns on structured outputs — the API constrains the
    response to that JSON Schema rather than the prompt merely asking for JSON.

    Why it matters here: this is the FIRST link in the live chain, and until now it was
    the only link that could not be asked for JSON at all — `json_mode` was not even a
    parameter, so `_json_call` set it and this function ignored it. Every JSON guarantee
    rested on the words 'Return ONLY raw JSON' in a prompt plus _loads_lenient cleaning up
    afterwards. That holds while there is something to extract and fails when there is
    not: a model with no signal to report explains itself in prose instead, which is the
    no-signal case that returned prose five times out of five."""
    client = _anthropic_client()
    _stats_call(f"anthropic:{model or model_for('anthropic')}", len(system or DEFAULT_SYSTEM) + len(user))
    kw = {}
    if schema:
        kw["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    effort = getattr(_EFFORT_TL, "effort", None)
    if effort and _thinking_headroom(model):          # effort exists only on thinking models
        kw.setdefault("output_config", {})["effort"] = effort
    # max_tokens was hardcoded at 4000, which ignored every caller's ceiling — a judge
    # call asking for 500 was allocated 4000.
    # `model` is the chain link's model. Before 2026-09-24 this always sent
    # model_for("anthropic"), so an explicit model= (the Sonnet judges) silently ran on Opus.
    msg = client.messages.create(model=model or model_for("anthropic"),
                                 max_tokens=int(max_tokens or MAXTOK_EXTRACT) + _thinking_headroom(model),
                                 system=system or DEFAULT_SYSTEM,
                                 messages=[{"role": "user", "content": user}], **kw)
    # Take the first TEXT block rather than content[0]: a model configured with thinking
    # returns a thinking block first, and indexing blindly would read the wrong one.
    text = next((b.text for b in msg.content if getattr(b, "type", None) == "text"), "")
    u = getattr(msg, "usage", None)
    _stats_usage({"prompt_tokens": getattr(u, "input_tokens", 0) or 0,
                  "completion_tokens": getattr(u, "output_tokens", 0) or 0,
                  "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                  "cache_creation_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0}
                 if u else None, len(text))
    label = f"anthropic:{model or model_for('anthropic')}"
    # stop_reason decides whether the text may be read at all. Before 2026-09-25 it was
    # only logged when the text was empty, so a reply cut off at max_tokens went to the
    # lenient parser and a refusal (empty text) fell through to the next link unremarked.
    stop = getattr(msg, "stop_reason", None)
    if stop == "max_tokens":
        _stats_bump("truncations")
        raise _Truncated(f"{label}: reply hit the output cap ({int(max_tokens or MAXTOK_EXTRACT)} tokens)")
    if stop == "refusal":
        _stats_bump("refusals")
        raise _Refused(f"{label}: the model declined ({getattr(msg, 'stop_details', None) or 'no details'})")
    if not text:
        print(f"[i] {label} returned no text (stop_reason={stop or '?'}); next link…", file=sys.stderr)
    return text


# Default model chain, best→most-reliable. Every call walks this until one link
# succeeds, so a slow/rate-limited/JSON-flaky provider never fails the run: the strong,
# no-rate-limit models lead; NIM's clean-JSON llama is the backstop that's almost never
# empty. Links whose API key is absent are dropped. Override with BRIEF_MODEL_CHAIN.
_DEFAULT_CHAIN = [
    ("cerebras", "gpt-oss-120b"),                  # fast, no rate limit, strong generation
    ("cerebras", "zai-glm-4.7"),                   # 2nd Cerebras model (different failure mode)
    ("groq",     "openai/gpt-oss-120b"),           # same strong model, different host
    ("groq",     "llama-3.3-70b-versatile"),       # fast, clean JSON
    # NIM backstop, re-verified 2026-09-22 by probing every link on this account.
    # The two that used to sit here were both dead and had been for weeks, silently,
    # because the chain only reaches them when the lead fails:
    #   meta/llama-3.3-70b-instruct          410 Gone — EOL 2026-08-26, gone from the catalog
    #   nvidia/llama-3.1-nemotron-70b-instr  404 — IN the catalog, not entitled to this account
    # Those are different problems with the same log line, which is why neither was noticed.
    # Catalog presence does not imply entitlement: check with a real call, not `list_models`.
    ("nim",      "nvidia/nemotron-3-super-120b-a12b"),   # probed clean JSON
    ("nim",      "openai/gpt-oss-20b"),                  # probed clean JSON, different family
    # Rejected after probing: nemotron-3.5-lightning-30b-a3b leaks its reasoning preamble
    # (the enable_thinking guard keys off "reasoning"/"thinking" in the NAME and this has
    # neither), and nemotron-3-ultra-550b-a55b returned a corrupted key: {"ok{": true}.
]
_KEY_ENV = {"cerebras": "CEREBRAS_API_KEY", "groq": "GROQ_API_KEY", "nim": "NVIDIA_API_KEY",
            "gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
            "ollama": None}
_CHAIN_LOGGED = False
# Circuit-breaker: a link that 429s is skipped for this many seconds so subsequent calls
# settle onto a free link instead of re-failing the saturated lead on every single call.
_LINK_COOLDOWN = {}          # "provider:model" -> monotonic deadline
_COOLDOWN_SECS = float(os.environ.get("BRIEF_LINK_COOLDOWN", "45"))


def _allow_nonclaude() -> bool:
    """True when BRIEF_ALLOW_NONCLAUDE is set (1/true/yes): non-Claude links may stay in a
    chain led by Claude. Off by default since 2026-09-25."""
    return os.environ.get("BRIEF_ALLOW_NONCLAUDE", "").strip().lower() in ("1", "true", "yes")


def _cooldown(provider, model):
    """Put the provider:model link on cooldown after an HTTP 429: store a monotonic
    deadline _COOLDOWN_SECS from now (BRIEF_LINK_COOLDOWN, default 45 s) in _LINK_COOLDOWN.
    _model_chain drops links that are still cooling, unless every link is."""
    import time
    _LINK_COOLDOWN[f"{provider}:{model}"] = time.monotonic() + _COOLDOWN_SECS


def _provider_for_model(m: str) -> "str | None":
    """Best-guess host for an explicitly-pinned model id (a wrong guess self-heals — the
    chain falls through to the next link if the pinned link errors)."""
    if m.startswith("claude-"):
        return "anthropic"
    if m.startswith(("nvidia/", "meta/")):
        return "nim"
    if m in ("gpt-oss-120b", "zai-glm-4.7"):
        return "cerebras"
    if m.startswith(("openai/", "meta-llama/", "qwen/")) or m.endswith("-versatile"):
        return "groq"
    return None


def _model_chain(model=None) -> list:
    """Ordered [(provider, model)] to try, best first. BRIEF_MODEL_CHAIN overrides the
    default; an explicit BRIEF_PROVIDER (and model=) is honoured as the FIRST link, with
    the rest kept as fallback so a pinned provider still never fails the run."""
    global _CHAIN_LOGGED
    env_chain = os.environ.get("BRIEF_MODEL_CHAIN", "").strip()
    if env_chain:
        chain = [tuple(t.strip().split(":", 1)) for t in env_chain.split(",")
                 if ":" in t]
    else:
        chain = [(p, m) for (p, m) in _DEFAULT_CHAIN
                 if p == "ollama" or os.environ.get(_KEY_ENV.get(p) or "")]
    # Honour an explicit pin (BRIEF_PROVIDER / model=) as the lead link, keep fallback.
    pin_p = os.environ.get("BRIEF_PROVIDER", "").lower().strip()
    pin_m = model or os.environ.get("BRIEF_MODEL", "")
    if pin_p:
        # With no explicit model, the pinned provider's model comes from the chain itself
        # (BRIEF_MODEL_CHAIN), not model_for()'s default. Before 2026-09-24 a pin replaced
        # "anthropic:claude-opus-5-5" in the chain with the default claude-opus-4-6.
        pin_m = pin_m or next((m for p, m in chain if p == pin_p), "")
        host = _provider_for_model(model) if model else None     # e.g. gpt-oss under an anthropic pin
        lead = (host or pin_p, pin_m or model_for(pin_p))
        chain = [lead] + [l for l in chain if l != lead]
    elif model:
        prov = _provider_for_model(model) or (chain[0][0] if chain else resolve_provider())
        lead = (prov, model)
        chain = [lead] + [l for l in chain if l != lead]
    # Claude-only by default. With the lead link on Claude, a non-Claude fallback link
    # meant that an API key with no credit produced a brief written by a NIM model (whose
    # terms exclude production use) and labelled as Claude (audit N2/C6). BRIEF_ALLOW_NONCLAUDE=1
    # keeps the old behaviour for someone who wants the backstop. Sai, 2026-09-25: Claude
    # only; testing runs on the Claude Code login.
    if chain and chain[0][0] == "anthropic" and not _allow_nonclaude():
        chain = [l for l in chain if l[0] == "anthropic"]
    if not _CHAIN_LOGGED and chain:
        print("[i] model chain: " + " → ".join(f"{p}:{m}" for p, m in chain)
              + ("" if _allow_nonclaude() or chain[0][0] != "anthropic"
                 else "  (Claude only; BRIEF_ALLOW_NONCLAUDE=1 to allow other links)"), file=sys.stderr)
        _CHAIN_LOGGED = True
    # Drop links that are mid-cooldown (recently 429'd); if every link is cooling, keep the
    # full chain rather than deadlock — something is better than heuristic.
    import time
    now = time.monotonic()
    live = [l for l in chain if _LINK_COOLDOWN.get(f"{l[0]}:{l[1]}", 0.0) <= now]
    return live or chain


# BRIEF_CLAUDE_TRANSPORT=cli sends every `anthropic:` link through the Claude Code CLI
# (`claude -p`, the logged-in Claude Code account) instead of the API key — the route
# serve.py already uses for the mock agent. Chains, model pins, labels and pricing are
# unchanged, so a run is comparable with an API run; only the transport differs. Parity
# with the API path: no tools, one turn, no session file, no user/project settings or MCP
# servers; thinking off for models that do not think by default on the API (Opus 4.6), and
# the API's default effort for those that do (Opus 5.5 medium, others high); the caller's
# max_tokens (plus the same thinking headroom) as the output cap.
CLI_TIMEOUT_S = float(os.environ.get("BRIEF_CLI_TIMEOUT", "240"))
# A --json-schema call answers through Claude Code's structured-output tool, which takes a
# turn of its own, and takes another when its first reply does not match the schema. At
# --max-turns 1 such a call ended "claude CLI failed (exit 1)" with stop_reason tool_use
# (2026-09-28: 2 of 3 judge rounds lost, a synthesis pushed to its fallback). With no
# other tools offered (--tools ""), extra turns can only be structured-output attempts.
CLI_SCHEMA_TURNS = "3"
_CLI_DEFAULT_EFFORT = {"claude-opus-5-5": "medium"}


# ---------------------------------------------------------------------------
# Model routes (Sai, 2026-09-26; ADR 0011). Each call names its JOB, and the job picks
# the model: one model everywhere forced one trade-off everywhere (Opus 4.6 invents
# figures in RTBs; Opus 5.5 thinks on a 7-second competitor lookup). Rules: a judge is
# never the model that wrote what it judges; extraction stays on the strongest writer
# until an A/B shows another model keeps full capture coverage. Each route is
# [model, fallback], Claude only. BRIEF_ROUTE_<JOB>="model[,fallback]" overrides one
# route; BRIEF_ROUTES=0, an explicit BRIEF_MODEL or BRIEF_MODEL_CHAIN, or a non-Anthropic provider falls back
# to the single-model chain (the whole-pipeline swaps the comparisons use).
# ---------------------------------------------------------------------------
HAIKU = "claude-haiku-4-5-20251001"
ROUTES = {
    "extract":         ("claude-opus-4-6", "claude-opus-5-5"),   # capture, golden extraction
    "hero":            ("claude-opus-4-6", "claude-opus-5-5"),   # insight/SMP drafts, refine, other fields
    "grounded_writer": ("claude-opus-5-5", "claude-opus-4-6"),   # RTB, desired response: 0/8 vs 11 invented figures
    "hero_judge":      ("claude-opus-5-5", "claude-sonnet-5"),   # insight/SMP judges, territory map
    "judge":           ("claude-sonnet-5", HAIKU),               # every other field's judge
    "mechanical":      ("claude-sonnet-5", HAIKU),               # scorecard, how-to-win, rerank
    "synth":           ("claude-sonnet-5", HAIKU),               # loop syntheses (Sai: Sonnet, not Haiku)
}
# Effort per job on thinking models (Opus 5.5, Sonnet 5): judges and mechanical calls
# answer a rule, so they think little. None = the model's own default.
ROUTE_EFFORT = {"hero_judge": "low", "judge": "low", "mechanical": "low", "synth": "medium"}
GROUNDED_FIELDS = ("reasons_to_believe", "desired_response")
HERO_FIELDS = ("insight", "smp")
_EFFORT_TL = threading.local()       # the effort of the call in flight on this thread


def routes_active() -> bool:
    """True when calls are routed by job: BRIEF_ROUTES is not 0, no BRIEF_MODEL or
    BRIEF_MODEL_CHAIN pins the whole pipeline, and the lead provider is Anthropic."""
    return (os.environ.get("BRIEF_ROUTES", "1") != "0" and not os.environ.get("BRIEF_MODEL")
            and not os.environ.get("BRIEF_MODEL_CHAIN", "").strip()
            and resolve_provider() == "anthropic")


def route_models(route: "str | None", exclude: "str | None" = None) -> "list | None":
    """The models for a job, lead first (BRIEF_ROUTE_<JOB> overrides the table), minus
    `exclude` (a judge's writer). None when routing is off or the job is unknown; [] when
    the exclusion left nothing, which the caller treats as no judge available."""
    if not route or route not in ROUTES or not routes_active():
        return None
    env = os.environ.get(f"BRIEF_ROUTE_{route.upper()}", "").strip()
    models = [m.strip() for m in env.split(",") if m.strip()] if env else list(ROUTES[route])
    return [m for m in dict.fromkeys(models) if m != exclude]


def writer_route(field_id: str) -> str:
    """The job that writes a golden field: grounded_writer for RTB and desired response,
    hero for everything else."""
    return "grounded_writer" if field_id in GROUNDED_FIELDS else "hero"


def judge_route(field_id: str) -> str:
    """The job that judges a golden field: hero_judge for insight and SMP, judge otherwise."""
    return "hero_judge" if field_id in HERO_FIELDS else "judge"


def model_routes() -> dict:
    """Every job's models as this run resolves them, for meta.model_routes ({} when off)."""
    return {r: route_models(r) for r in ROUTES} if routes_active() else {}


# Set once `auto` has switched this process to the CLI (credit/auth failure on the API).
# The switch expires after CLI_FALLBACK_TTL_S so a server re-checks the API instead of
# staying on the slower CLI until restart after one auth hiccup (audit critic-G11).
_CLI_FALLBACK = {"on": False, "since": 0.0}
_CLI_FALLBACK_LOCK = threading.Lock()
CLI_FALLBACK_TTL_S = float(os.environ.get("BRIEF_CLI_FALLBACK_TTL", "600"))


def _switch_to_cli(reason: str) -> None:
    """Move every later `anthropic:` call in this process to the CLI (transport `auto`)
    for CLI_FALLBACK_TTL_S seconds, announcing it once on stderr. Idempotent and
    thread-safe: parallel calls that fail at the same moment switch once and print once."""
    import time
    with _CLI_FALLBACK_LOCK:
        if _cli_fallback_active():
            return
        _CLI_FALLBACK["on"] = True
        _CLI_FALLBACK["since"] = time.monotonic()
    print(f"[!] BRIEF_CLAUDE_TRANSPORT=auto: the API key cannot be used ({reason}); "
          f"this process continues on the Claude Code CLI for {int(CLI_FALLBACK_TTL_S)} s, "
          f"then re-tries the API.", file=sys.stderr)


def _cli_fallback_active() -> bool:
    """True while an `auto` switch to the CLI is in force (set and younger than the TTL)."""
    import time
    if not _CLI_FALLBACK.get("on"):
        return False
    if time.monotonic() - float(_CLI_FALLBACK.get("since") or 0.0) > CLI_FALLBACK_TTL_S:
        _CLI_FALLBACK["on"] = False
        return False
    return True


def _claude_transport() -> str:
    """The transport for `anthropic:` links, from BRIEF_CLAUDE_TRANSPORT:
      api   the API key (the default; unchanged behaviour),
      cli   the Claude Code CLI (`claude -p`, the logged-in account),
      auto  the API, switching this process to the CLI on a credit or auth failure, or
            straight away when no ANTHROPIC_API_KEY is set but `claude` is installed.
    Anything else reads as 'api', so a typo never silently moves traffic."""
    t = os.environ.get("BRIEF_CLAUDE_TRANSPORT", "").strip().lower()
    return t if t in ("api", "cli", "auto") else "api"


def _api_account_failure(exc: Exception) -> bool:
    """True when an API error means the KEY cannot be used (no credit, bad or missing key),
    as opposed to a transient or request error that the next chain link should handle."""
    name, msg = exc.__class__.__name__, str(exc)
    return (name in ("AuthenticationError", "PermissionDeniedError")
            or (name == "BadRequestError" and "credit balance" in msg.lower())
            or isinstance(exc, KeyError) and "ANTHROPIC_API_KEY" in msg)


def transport_used() -> str:
    """What `anthropic:` links actually ran on in this process: 'api', 'cli', or
    'api→cli' once `auto` has switched. Written into run outputs so API and Claude Code
    runs are never compared as if they were the same thing."""
    t = _claude_transport()
    if t == "auto":
        return "api→cli" if _cli_fallback_active() else "api"
    return t


_CLI_WORKDIR: str | None = None


def _cli_workdir() -> str:
    """The folder every `claude -p` call runs in: an empty temp folder made once per
    process. Claude Code adds context about the folder it starts in, and from ~ (used until
    2026-09-29) that included the home folder's auto-memory (MEMORY.md with personal notes),
    about 110 input tokens on every brief call and text the brief has no business seeing
    (audit N7, measured through a logging proxy). What remains cannot be switched off without
    --bare, which needs an API key: a billing line, an Agent SDK identity line, the folder,
    platform, model name, account email and date, about 375 tokens a call."""
    global _CLI_WORKDIR
    if _CLI_WORKDIR is None or not os.path.isdir(_CLI_WORKDIR):
        import tempfile
        _CLI_WORKDIR = tempfile.mkdtemp(prefix="napkin-engine-cli-")
    return _CLI_WORKDIR


def _chat_claude_cli(user, system=None, max_tokens=None, schema=None, model=None):
    """The Anthropic link over the Claude Code CLI: one `claude -p` call, prompt on stdin.

    Returns the reply text (for a `schema` call, the CLI's validated structured output
    re-serialised as JSON, so _json_call parses it the same way). Raises _RateLimited when
    the account's usage limit is hit and RuntimeError on any other failure, so the chain
    advances exactly as it does for an API error. Usage is recorded under the same
    `anthropic:<model>` label as the API path, with the CLI's own token counts."""
    import shutil
    import subprocess
    model = model or model_for("anthropic")
    if not shutil.which("claude"):
        raise RuntimeError("claude CLI not on PATH (BRIEF_CLAUDE_TRANSPORT=cli)")
    _stats_call(f"anthropic:{model}", len(system or DEFAULT_SYSTEM) + len(user))
    cmd = ["claude", "-p", "--model", model, "--system-prompt", system or DEFAULT_SYSTEM,
           "--tools", "", "--max-turns", CLI_SCHEMA_TURNS if schema else "1", "--output-format", "json",
           "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config"]
    env = {**os.environ,
           "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(int(max_tokens or MAXTOK_EXTRACT) + _thinking_headroom(model))}
    if _thinking_headroom(model):
        cmd += ["--effort", os.environ.get("BRIEF_CLI_EFFORT") or getattr(_EFFORT_TL, "effort", None)
                or _CLI_DEFAULT_EFFORT.get(model, "high")]
    else:
        env["MAX_THINKING_TOKENS"] = "0"          # the API path does not think on these models
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    # The CLI must not inherit an API key: with one set it bills the key (which may have no
    # credit) instead of the logged-in account this transport exists to use.
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        proc = subprocess.run(cmd, input=user, capture_output=True, text=True,
                              timeout=CLI_TIMEOUT_S, env=env, cwd=_cli_workdir())
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"claude CLI timed out after {CLI_TIMEOUT_S:.0f}s") from e
    try:
        env_out = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        env_out = {}
    if proc.returncode != 0 or env_out.get("is_error") or env_out.get("subtype") not in (None, "success"):
        detail = str(env_out.get("result") or proc.stderr or proc.stdout or "")[:300]
        if re.search(r"usage limit|rate limit|429|overloaded", detail, re.I):
            raise _RateLimited(f"claude CLI: {detail}")
        raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {detail}")
    so = env_out.get("structured_output")
    text = json.dumps(so, ensure_ascii=False) if (schema and so is not None) else str(env_out.get("result") or "")
    u = env_out.get("usage") or {}
    # Cache reads and writes are recorded apart from the uncached input: they are priced
    # differently (reads at 0.1x), and folding them into prompt_tokens overstated CLI runs
    # by ~35-40% (audit BW13/CC9).
    _stats_usage({"prompt_tokens": int(u.get("input_tokens") or 0),
                  "completion_tokens": int(u.get("output_tokens") or 0),
                  "cache_read_tokens": int(u.get("cache_read_input_tokens") or 0),
                  "cache_creation_tokens": int(u.get("cache_creation_input_tokens") or 0)}, len(text))
    stop = env_out.get("stop_reason")
    if stop == "max_tokens" or re.search(r"exceeded .*output token", text[:200], re.I):
        _stats_bump("truncations")
        raise _Truncated(f"claude-cli:{model}: reply hit the output cap")
    if stop == "refusal":
        _stats_bump("refusals")
        raise _Refused(f"claude-cli:{model}: the model declined")
    if not text:
        print(f"[i] claude-cli:{model} returned no text (stop_reason={stop or '?'}); next link…",
              file=sys.stderr)
    return text


def _call_link(provider: str, model: str, user, system=None, max_tokens=None,
               json_mode=False, schema=None) -> "str | None":
    """Call exactly ONE (provider, model) link. Raises on failure so the chain advances.
    An `anthropic` link goes over the API or the Claude Code CLI per BRIEF_CLAUDE_TRANSPORT
    (see _claude_transport and _chat_claude_cli)."""
    if provider == "anthropic":
        kw = dict(system=system, max_tokens=max_tokens, schema=schema if json_mode else None, model=model)
        t = _claude_transport()
        if t == "cli" or (t == "auto" and _cli_fallback_active()):
            return _chat_claude_cli(user, **kw)
        if t == "auto" and not os.environ.get("ANTHROPIC_API_KEY"):
            import shutil
            if shutil.which("claude"):
                _switch_to_cli("no ANTHROPIC_API_KEY set")
                return _chat_claude_cli(user, **kw)
        try:
            return _chat_anthropic(user, **kw)
        except Exception as e:
            if t == "auto" and _api_account_failure(e):
                _switch_to_cli(f"{e.__class__.__name__}: {str(e)[:120]}")
                return _chat_claude_cli(user, **kw)
            raise
    cfg = PROVIDERS.get(provider)
    if not cfg:
        raise RuntimeError(f"unknown provider '{provider}'")
    default_base, key_env, _ = cfg
    key = os.environ.get(key_env) if key_env else "ollama"
    if key_env and not key:
        raise RuntimeError(f"{key_env} not set")
    return _chat_openai_compatible(default_base, key, model, user,
                                   provider_label=f"{provider}:{model}", system=system,
                                   max_tokens=max_tokens, json_mode=json_mode, schema=schema)


def _stats_logical():
    """One LOGICAL call (one _chat/_json_call) may cost several link attempts when the
    chain fails over — 'calls' counts attempts, this counts intent."""
    _stats_bump("logical_calls")


def _chat(user, system=None, model=None, max_tokens=None):
    """Provider-agnostic chat. Walks the model chain (see _model_chain) and returns the
    first link's raw text, or None if every link failed (callers fall back to heuristic)."""
    _stats_logical()
    _EFFORT_TL.effort = None
    for provider, m in _model_chain(model):
        try:
            raw = _call_link(provider, m, user, system=system, max_tokens=max_tokens)
            if raw:
                _note_answer(provider, m)
                return raw
        except _RateLimited:                     # saturated — cool it down so later calls skip it
            _cooldown(provider, m)
            print(f"[i] link {provider}:{m} rate-limited; cooling {int(_COOLDOWN_SECS)}s, next link…",
                  file=sys.stderr)
        except Exception as e:                   # network/auth/HTTP — try the next link
            print(f"[i] link {provider}:{m} failed ({e.__class__.__name__}); next link…",
                  file=sys.stderr)
    return None


_NONCLAUDE_WARNED = threading.local()


def _note_answer(provider: str, m: str):
    """Record the link whose reply is being used, and say so loudly the first time a
    non-Claude link answers under a Claude lead (once per thread-ledger, i.e. per run)."""
    label = f"{provider}:{m}"
    _stats_answered(label)
    if provider != "anthropic" and resolve_provider() == "anthropic":
        led = _ledger()
        if not led.get("nonclaude_warned"):
            led["nonclaude_warned"] = True
            print(f"[!] a non-Claude link answered ({label}) — this brief is not a Claude brief; "
                  f"meta.fallback_links records it.", file=sys.stderr)


def _json_call(user, system=None, retries=1, model=None, accept=None, max_tokens=None,
               schema=None, parse=None, whole=False, only_model=False, info=None,
               route=None, exclude=None):
    """Chat call that must return JSON, with provider juggling: each chain link gets up to
    `retries`+1 tries; unparseable output (or one rejected by `accept`) advances to the next
    link. Returns the first usable object, or None if the whole chain is exhausted.
    `parse` replaces the JSON reader for a non-JSON reply format (the TOON calls pass
    _loads_toon) and turns the providers' JSON mode off; it returns None on failure.
    `whole=True` reads only a complete top-level object (no inner-span salvage): for
    tournaments, judges and the critic, where a partial reply must never pass as a whole one.
    `only_model=True` walks the pinned `model` alone (over api or cli), never a fallback
    link: the critic and the eval judges must answer on their own model or not at all.
    `info`, when a dict, receives {"link": "<provider:model>"} of the link that answered.
    A reply cut off at the output cap is retried once with 1.5x the cap on the same link,
    then the next link; a refusal is logged and counted, then the next link."""
    _stats_logical()
    routed = None if (model or only_model) else route_models(route, exclude)
    if only_model and model:
        chain = [(_provider_for_model(model) or "anthropic", model)]
    elif routed is not None:
        # A routed call walks its job's own models (ADR 0011), not the default chain, so
        # a judge can never fall back onto its writer. Cooling links are skipped unless
        # every one is cooling.
        import time as _t
        now = _t.monotonic()
        chain = [("anthropic", m) for m in routed]
        chain = [l for l in chain if _LINK_COOLDOWN.get(f"{l[0]}:{l[1]}", 0.0) <= now] or chain
        if not chain:
            print(f"[!] route {route}: no model left after excluding the writer ({exclude}); unjudged.",
                  file=sys.stderr)
            return None
    else:
        chain = _model_chain(model)
    _EFFORT_TL.effort = ROUTE_EFFORT.get(route) if routed is not None else None
    try:
        reader = parse or (functools.partial(_loads_lenient, whole=True) if whole else _loads_lenient)
        for provider, m in chain:
            cap, grew = max_tokens, False
            attempt = 0
            while attempt <= retries:
                try:
                    raw = _call_link(provider, m, user, system=system, max_tokens=cap,
                                     json_mode=parse is None, schema=schema)
                except _RateLimited:
                    _cooldown(provider, m)
                    print(f"[i] link {provider}:{m} rate-limited; cooling {int(_COOLDOWN_SECS)}s, next link…",
                          file=sys.stderr)
                    break
                except _Truncated as e:
                    if not grew:
                        cap, grew = int((cap or MAXTOK_EXTRACT) * 1.5), True
                        print(f"[i] {e}; retrying once with {cap} tokens.", file=sys.stderr)
                        continue                     # same link, more room, not counted as a retry
                    print(f"[!] {e} again at {cap} tokens; next link…", file=sys.stderr)
                    break
                except _Refused as e:
                    print(f"[!] {e}; next link…", file=sys.stderr)
                    break
                except Exception as e:
                    print(f"[i] link {provider}:{m} failed ({e.__class__.__name__}); next link…",
                          file=sys.stderr)
                    break                            # this link is down — go to the next one
                if not raw:
                    break
                raw = re.sub(r"^```(?:json|toon)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
                obj = reader(raw)
                if obj is not None and (accept is None or accept(obj)):
                    _note_answer(provider, m)
                    if isinstance(info, dict):
                        info["link"] = f"{provider}:{m}"
                    return obj
                attempt += 1
                if attempt <= retries:
                    print(f"[i] {provider}:{m} unclean/unusable JSON; retrying once.", file=sys.stderr)
            # link exhausted → fall through to the next provider in the chain
        print("[i] no provider in the chain returned usable JSON.", file=sys.stderr)
        return None
    finally:
        _EFFORT_TL.effort = None           # never leaks into the next call on this thread


def _loads_lenient(raw, whole=False):
    """Reasoning models sometimes wrap the JSON in prose. Try a clean parse,
    then every balanced {...} span (largest first), each with a trailing-comma
    repair pass. Returns dict/list or None.
    `whole=True` stops after the clean parse: no inner span is salvaged, so a reply cut
    off part-way (a truncated tournament, judge or critic) reads as unusable rather than
    as the one complete object inside it (audit F8/G4)."""
    def _try(s):
        """Parse `s` as JSON, then once more with trailing commas before } or ]
        removed. Returns the parsed value, or None when both attempts fail."""
        for candidate in (s, re.sub(r",\s*([}\]])", r"\1", s)):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
        return None

    obj = _try(raw)
    if obj is not None:
        return obj
    if whole:
        return None
    spans = []
    start = raw.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_str:
                if esc:        esc = False
                elif ch == "\\": esc = True
                elif ch == '"': in_str = False
            elif ch == '"':    in_str = True
            elif ch == "{":    depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    spans.append(raw[start:i + 1])
                    break
        start = raw.find("{", start + 1)
    for span in sorted(set(spans), key=len, reverse=True):
        obj = _try(span)
        if isinstance(obj, dict):
            return obj
    return None


# Names parse_brief re-exports and forwards assignments for (see parse_brief._ForwardingModule).
MOVED_NAMES = (
    'CLI_FALLBACK_TTL_S',
    'CLI_TIMEOUT_S',
    'GROUNDED_FIELDS',
    'HAIKU',
    'HERO_FIELDS',
    'LINK_TIMEOUT_S',
    'MAXTOK_EXTRACT',
    'MAXTOK_GEN',
    'MAXTOK_JUDGE',
    'MAXTOK_SYNTH_ONE',
    'NoClaudeAvailable',
    'PROVIDERS',
    'ROUTES',
    'ROUTE_EFFORT',
    'THINKING_HEADROOM',
    '_CHAIN_LOGGED',
    '_CLI_DEFAULT_EFFORT',
    '_CLI_FALLBACK',
    '_CLI_FALLBACK_LOCK',
    '_COOLDOWN_SECS',
    '_DEFAULT_CHAIN',
    '_EFFORT_TL',
    '_EMPTY_STATS',
    '_HTTP_UA',
    '_KEY_ENV',
    '_LINK_COOLDOWN',
    '_LLM_STATS',
    '_NONCLAUDE_WARNED',
    '_NoEvidence',
    '_RateLimited',
    '_Refused',
    '_STATS_LOCK',
    '_STATS_TL',
    '_THINKING_MODELS',
    '_Truncated',
    '_allow_nonclaude',
    '_api_account_failure',
    '_call_link',
    '_chat',
    '_chat_anthropic',
    '_chat_claude_cli',
    '_chat_openai_compatible',
    '_claude_transport',
    '_cli_fallback_active',
    '_cooldown',
    '_json_call',
    '_ledger',
    '_loads_lenient',
    '_model_chain',
    '_note_answer',
    '_provider_for_model',
    '_scoped',
    '_stats_answered',
    '_stats_bump',
    '_stats_call',
    '_stats_logical',
    '_stats_reset',
    '_stats_scope',
    '_stats_snapshot',
    '_stats_usage',
    '_switch_to_cli',
    '_thinking_headroom',
    'judge_route',
    'list_models',
    'model_for',
    'model_routes',
    'resolve_provider',
    'route_models',
    'routes_active',
    'transport_used',
    'writer_route',
)
