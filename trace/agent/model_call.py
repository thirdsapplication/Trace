"""One place that knows how to call the Messages API across SDK versions AND models.

Five engines each build their own caller and every one passes ``temperature=0`` for
determinism. When the SDK went to 1.x it stopped accepting that keyword, and all five
broke at once -- in production, with nothing in this repo having changed. The pin in
requirements.txt is the real fix; this is so the next signature change degrades
instead of failing.

The same failure came back a second way with the learning engine: newer models
(Fable 5 and the rest of the 4.7+/5 family) REMOVED sampling parameters -- the SDK
still accepts the keyword, but the API answers 400. That is not a TypeError, so the
original fallback never fired and every learning call died with no run. Hence the
second fallback below, and ``temperature=None`` for callers that know their model
must never see the parameter.

Determinism is preferred, never required: a run without temperature is worth more
than no run at all, and the alternative is an agent build that dies on a keyword.
"""


def create_text(client, model, system, user, max_tokens=4000, temperature=0):
    """Call Messages and return the concatenated text.

    Sends ``temperature`` when the SDK and model accept it and retries without when
    either does not -- distinguishing THOSE errors from genuine ones by the message,
    so a real bug still raises rather than being retried into silence.
    ``temperature=None`` omits the parameter entirely (for models like Fable 5 that
    reject all sampling parameters), the same way an absent ``system`` is omitted
    rather than sent as None.
    """
    kwargs = dict(model=model, max_tokens=max_tokens,
                  messages=[{"role": "user", "content": user}])
    if system:                       # the scorer has no system prompt; sending
        kwargs["system"] = system    # system=None is not the same as omitting it
    if temperature is None:
        resp = client.messages.create(**kwargs)
        return _text(resp)
    try:
        resp = client.messages.create(temperature=temperature, **kwargs)
    except TypeError as e:           # SDK signature drift (the 1.x incident)
        if "temperature" not in str(e):
            raise
        resp = client.messages.create(**kwargs)
    except Exception as e:           # noqa: BLE001 — API-level rejection (400 on 4.7+/5 models)
        msg = str(e).lower()
        if "temperature" not in msg and "sampling" not in msg:
            raise
        resp = client.messages.create(**kwargs)
    return _text(resp)


def _text(resp):
    return "".join(b.text for b in resp.content
                   if getattr(b, "type", None) == "text")
