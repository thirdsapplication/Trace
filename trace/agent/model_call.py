"""One place that knows how to call the Messages API across SDK versions.

Five engines each build their own caller and every one passes ``temperature=0`` for
determinism. When the SDK went to 1.x it stopped accepting that keyword, and all five
broke at once -- in production, with nothing in this repo having changed. The pin in
requirements.txt is the real fix; this is so the next signature change degrades
instead of failing.

Determinism is preferred, never required: a run without temperature is worth more
than no run at all, and the alternative is an agent build that dies on a keyword.
"""


def create_text(client, model, system, user, max_tokens=4000, temperature=0):
    """Call Messages and return the concatenated text.

    Sends ``temperature`` when the SDK accepts it and retries without when it does
    not — distinguishing THAT TypeError from a genuine one by the message, so a real
    bug still raises rather than being retried into silence.
    """
    kwargs = dict(model=model, max_tokens=max_tokens,
                  messages=[{"role": "user", "content": user}])
    if system:                       # the scorer has no system prompt; sending
        kwargs["system"] = system    # system=None is not the same as omitting it
    try:
        resp = client.messages.create(temperature=temperature, **kwargs)
    except TypeError as e:
        if "temperature" not in str(e):
            raise
        resp = client.messages.create(**kwargs)
    return "".join(b.text for b in resp.content
                   if getattr(b, "type", None) == "text")
