"""The jev port: TypeSafe's jev answers yes/no ("noul") and multiple-choice questions about
one text with a calibrated probability, fast (well under a second) and cheaply. It cannot
write; it checks and chooses. Brief Maker's check loop asks it which parts need rewriting.

    JevPort(api_key, model="jev-latest", base_url=None, timeout=20)
    .ask(state, questions) -> {name: probability} for nouls, {name: (choice, {option: p})} for choices

`questions` are raw SDK dictionaries: {"type": "noul" | "choice", "instructions": {...},
"criteria": {...}}. At most BATCH per request (the vendor accepts 50). Raises JevError on any
failure; a caller treats that as "not checked", never as a verdict.
"""
from __future__ import annotations

import threading

BATCH = 40
STATE_CHARS = 60_000


class JevError(Exception):
    pass


def noul(question: str, true: str, false: str, **instructions) -> dict:
    return {"type": "noul", "instructions": {"question": question, **instructions},
            "criteria": {"true": true, "false": false}}


def choice(question: str, options: dict, **instructions) -> dict:
    return {"type": "choice", "instructions": {"question": question, **instructions}, "criteria": dict(options)}


class JevPort:
    def __init__(self, api_key: str, model: str = "jev-latest", base_url: str | None = None, timeout: float = 20.0,
                 client=None):
        self.model, self.timeout = model, timeout
        self._lock = threading.Lock()  # one request at a time per client (the SDK's SSL note)
        if client is not None:
            self._client = client
            return
        try:
            import typesafe_sdk as sdk  # noqa: PLC0415 - only when jev is configured
        except ImportError as e:
            raise JevError("the jev SDK is not installed (typesafe-sdk==0.7.1)") from e
        kw = {"api_key": api_key, "model": model, "retry": sdk.RetryPolicy(max_retries=1)}
        if base_url:
            kw["base_url"] = base_url
        self._client = sdk.TypeSafeClient(**kw)

    def ask(self, state: dict, questions: dict) -> dict:
        names, out = list(questions), {}
        for i in range(0, len(names), BATCH):
            chunk = {n: questions[n] for n in names[i:i + BATCH]}
            try:
                with self._lock:
                    r = self._client.system_one(state=state, questions=chunk, model=self.model, timeout=self.timeout)
            except Exception as e:  # noqa: BLE001 - every failure is the one error
                raise JevError(f"jev did not answer ({type(e).__name__}: {str(e)[:160]})") from e
            for n, q in chunk.items():
                if q["type"] == "noul":
                    p = getattr((r.nouls or {}).get(n), "noul", None)
                    if not isinstance(p, (int, float)) or isinstance(p, bool) or not 0 <= p <= 1:
                        raise JevError(f"jev gave no probability for {n}")
                    out[n] = float(p)
                else:
                    a = (r.choices or {}).get(n)
                    if a is None:
                        raise JevError(f"jev gave no choice for {n}")
                    out[n] = (a.choice, {k: float(v) for k, v in dict(a.probabilities).items()})
        return out
