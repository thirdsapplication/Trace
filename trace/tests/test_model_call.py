"""One kwarg took out every engine at once, in production, with nothing changed here.

`anthropic` was pinned as `>=0.40.0` — unpinned — so Render's build pulled the newest
SDK. When it went to 1.x, `messages.create` stopped accepting `temperature`, and all
five engines pass `temperature=0` for determinism. Every agent build failed with
"Messages.create() got an unexpected keyword argument 'temperature'".

The pin in requirements.txt is the real fix. This is so the next signature change
degrades rather than failing: determinism is preferred, never required, because a run
without temperature is worth more than no run at all.

The module later absorbed a failure that is not a signature at all: above roughly 16K
max_tokens the SDK requires streaming, and an oversized non-streaming request does not
raise — it hangs to the HTTP timeout, so there is no error to read and nothing to fall
back to. ``create_text_streaming`` is that path, covered here because it has to stay
the SAME call underneath — text blocks only, system omitted rather than nulled — while
never sending the one parameter ``create_text`` still tries.
"""

import unittest

from trace.agent import model_call


class Block:
    def __init__(self, text):
        self.type, self.text = "text", text


class Resp:
    def __init__(self, text):
        self.content = [Block(text)]


class Messages:
    """An SDK that accepts temperature."""

    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return Resp("ok")


class MessagesWithoutTemperature(Messages):
    """The 1.x shape: the keyword is gone."""

    def create(self, **kw):
        if "temperature" in kw:
            raise TypeError(
                "Messages.create() got an unexpected keyword argument 'temperature'")
        return super().create(**kw)


class MessagesBrokenOtherwise(Messages):
    """A genuine bug, which must NOT be retried into silence."""

    def create(self, **kw):
        raise TypeError("create() got an unexpected keyword argument 'modle'")


class FakeAPIError(Exception):
    """An API-level rejection (the SDK raises its own classes; only the message
    matters to the fallback)."""


class MessagesRejectingSampling(Messages):
    """The Fable-5 shape: the SDK takes the keyword, the API answers 400."""

    def create(self, **kw):
        if "temperature" in kw:
            raise FakeAPIError(
                "Error code: 400 - temperature is not supported: sampling "
                "parameters have been removed on this model")
        return super().create(**kw)


class MessagesRejectingOtherwise(Messages):
    """A genuine API error, which must NOT be retried into silence."""

    def create(self, **kw):
        raise FakeAPIError("Error code: 400 - max_tokens exceeds model limit")


class Client:
    def __init__(self, messages):
        self.messages = messages


class Stream:
    """What ``.stream()`` hands back: a context manager, not a message. Leaving the
    with-block is the only thing that releases the HTTP connection, so the fake
    records whether it was ever closed."""

    def __init__(self, resp):
        self.resp, self.closed = resp, False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def get_final_message(self):
        return self.resp


class StreamingMessages:
    """An SDK with a working streaming transport. ``blocks`` stands in for what the
    stream accumulated before ``get_final_message`` assembled it."""

    blocks = ("streamed ", "ok")

    def __init__(self):
        self.calls, self.streams = [], []

    def stream(self, **kw):
        self.calls.append(kw)
        resp = Resp(self.blocks[0])
        for text in self.blocks[1:]:
            resp.content.append(Block(text))
        self.streams.append(Stream(resp))
        return self.streams[-1]


class ModelCallTest(unittest.TestCase):
    def test_the_default_sends_no_temperature(self):
        # Every engine runs on Fable 5.1, which 400s any sampling parameter, so the
        # default must not send one (or every call would pay a rejected round trip).
        m = Messages()
        out = model_call.create_text(Client(m), "m", "sys", "hi")
        self.assertEqual(out, "ok")
        self.assertEqual(len(m.calls), 1)
        self.assertNotIn("temperature", m.calls[0])

    def test_temperature_is_sent_when_the_sdk_takes_it(self):
        m = Messages()
        out = model_call.create_text(Client(m), "m", "sys", "hi", temperature=0)
        self.assertEqual(out, "ok")
        self.assertEqual(m.calls[0]["temperature"], 0)

    def test_it_retries_without_temperature_when_the_sdk_refuses(self):
        m = MessagesWithoutTemperature()
        out = model_call.create_text(Client(m), "m", "sys", "hi", temperature=0)
        self.assertEqual(out, "ok", "the whole build died here")
        self.assertNotIn("temperature", m.calls[-1])

    def test_a_real_typeerror_still_raises(self):
        """Retrying every TypeError would turn a typo into a silent second attempt
        that fails the same way, twice as slowly."""
        with self.assertRaises(TypeError):
            model_call.create_text(Client(MessagesBrokenOtherwise()), "m", "s", "u")

    def test_temperature_none_omits_the_parameter_in_one_call(self):
        """Fable 5 rejects all sampling parameters with a 400 — a caller that knows
        its model must be able to never send one, not send-and-retry."""
        m = Messages()
        out = model_call.create_text(Client(m), "m", "sys", "hi", temperature=None)
        self.assertEqual(out, "ok")
        self.assertEqual(len(m.calls), 1)
        self.assertNotIn("temperature", m.calls[0])

    def test_it_retries_without_temperature_when_the_api_refuses(self):
        """The 1.x incident, API-shaped: the SDK takes the keyword, the model 400s.
        Not a TypeError, so the original fallback never fired and every learning
        call died with no run."""
        m = MessagesRejectingSampling()
        out = model_call.create_text(Client(m), "m", "sys", "hi", temperature=0)
        self.assertEqual(out, "ok", "the whole learning cycle died here")
        self.assertNotIn("temperature", m.calls[-1])

    def test_a_real_api_error_still_raises(self):
        with self.assertRaises(FakeAPIError):
            model_call.create_text(Client(MessagesRejectingOtherwise()), "m", "s", "u")

    def test_an_absent_system_prompt_is_omitted_not_nulled(self):
        """The scorer has no system prompt, and system=None is not the same as
        leaving it out."""
        m = Messages()
        model_call.create_text(Client(m), "m", None, "hi", max_tokens=5)
        self.assertNotIn("system", m.calls[0])
        self.assertEqual(m.calls[0]["max_tokens"], 5)

    def test_only_text_blocks_are_returned(self):
        class Mixed(Messages):
            def create(self, **kw):
                r = Resp("kept")
                other = Block("dropped")
                other.type = "thinking"
                r.content.append(other)
                return r
        self.assertEqual(
            model_call.create_text(Client(Mixed()), "m", "s", "u"), "kept")


class StreamingCallTest(unittest.TestCase):
    """The scrub engine returns a verbatim rewrite as large as its input, which is
    what this transport exists for; a caller that quietly got create_text's behaviour
    instead would hang rather than fail."""

    def _client(self, messages=None):
        m = messages if messages is not None else StreamingMessages()
        return Client(m), m

    def test_the_accumulated_message_comes_back_as_one_string(self):
        c, m = self._client()
        self.assertEqual(model_call.create_text_streaming(c, "m", "sys", "hi"),
                         "streamed ok")
        # Same request shape as create_text: only the transport differs.
        self.assertEqual(m.calls[0]["model"], "m")
        self.assertEqual(m.calls[0]["messages"], [{"role": "user", "content": "hi"}])

    def test_the_stream_is_closed(self):
        """get_final_message() outside the with-block returns the same text and leaks
        the connection — the leak is invisible until a worker runs out of them."""
        c, m = self._client()
        model_call.create_text_streaming(c, "m", "sys", "hi")
        self.assertTrue(m.streams[0].closed)

    def test_no_temperature_is_sent_at_all(self):
        """Every model that needs this path (Fable 5, Sonnet 5, Opus 5) answers 400 to
        any sampling parameter, so unlike create_text there is nothing to send and no
        rejection worth a second round trip."""
        c, m = self._client()
        model_call.create_text_streaming(c, "m", "sys", "hi")
        self.assertNotIn("temperature", m.calls[0])
        self.assertEqual(len(m.calls), 1)

    def test_an_absent_system_prompt_is_omitted_not_nulled(self):
        c, m = self._client()
        model_call.create_text_streaming(c, "m", None, "hi")
        self.assertNotIn("system", m.calls[0])
        c, m = self._client()
        model_call.create_text_streaming(c, "m", "sys", "hi")
        self.assertEqual(m.calls[0]["system"], "sys")

    def test_max_tokens_is_passed_through(self):
        c, m = self._client()
        model_call.create_text_streaming(c, "m", "s", "u", max_tokens=64_000)
        self.assertEqual(m.calls[0]["max_tokens"], 64_000)

    def test_the_default_ceiling_is_the_one_create_text_could_not_pass(self):
        c, m = self._client()
        model_call.create_text_streaming(c, "m", "s", "u")
        self.assertEqual(m.calls[0]["max_tokens"], 16000)

    def test_only_text_blocks_are_returned(self):
        class Mixed(StreamingMessages):
            def stream(self, **kw):
                s = super().stream(**kw)
                other = Block("dropped")
                other.type = "thinking"
                s.resp.content.append(other)
                return s
        c, _ = self._client(Mixed())
        self.assertEqual(model_call.create_text_streaming(c, "m", "s", "u"),
                         "streamed ok")


if __name__ == "__main__":
    unittest.main()
