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

The third failure this module absorbs is size, not signature. Above roughly 16K
``max_tokens`` the SDK requires streaming, and a non-streaming request that large does
not fail loudly -- it hangs until the HTTP timeout, so there is no error to read and
nothing to fall back to. That ceiling is the only reason ``LEARNING_MAX_TOKENS`` is
pinned at 16000: ``create_text`` was all there was. The scrub engine cannot live under
it -- it returns a verbatim rewrite as large as its input (~25K tokens, up to 64K) --
so ``create_text_streaming`` is the same call with the streaming transport underneath.
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


def create_text_streaming(client, model, system, user, max_tokens=16000):
    """Call Messages over a stream and return the concatenated text.

    Same result as ``create_text`` -- one string, text blocks only -- for callers whose
    reply is too big to survive a non-streaming request (see the module docstring).

    Sends NO temperature at all -- not even the send-then-retry ``create_text`` does,
    because every model that needs this path (Fable 5, Sonnet 5, Opus 5) answers 400 to
    any sampling parameter: there is no determinism left to trade for, and no rejection
    worth a second round trip.
    """
    kwargs = dict(model=model, max_tokens=max_tokens,
                  messages=[{"role": "user", "content": user}])
    if system:                       # same rule as create_text: sending system=None
        kwargs["system"] = system    # is not the same as omitting it
    # .stream() hands back a manager, not a message: the with-block is what closes the
    # HTTP connection once get_final_message() has assembled the accumulated deltas.
    with client.messages.stream(**kwargs) as stream:
        resp = stream.get_final_message()
    return _text(resp)


def _text(resp):
    return "".join(b.text for b in resp.content
                   if getattr(b, "type", None) == "text")
