"""One kwarg took out every engine at once, in production, with nothing changed here.

`anthropic` was pinned as `>=0.40.0` — unpinned — so Render's build pulled the newest
SDK. When it went to 1.x, `messages.create` stopped accepting `temperature`, and all
five engines pass `temperature=0` for determinism. Every agent build failed with
"Messages.create() got an unexpected keyword argument 'temperature'".

The pin in requirements.txt is the real fix. This is so the next signature change
degrades rather than failing: determinism is preferred, never required, because a run
without temperature is worth more than no run at all.
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


class ModelCallTest(unittest.TestCase):
    def test_temperature_is_sent_when_the_sdk_takes_it(self):
        m = Messages()
        out = model_call.create_text(Client(m), "m", "sys", "hi")
        self.assertEqual(out, "ok")
        self.assertEqual(m.calls[0]["temperature"], 0)

    def test_it_retries_without_temperature_when_the_sdk_refuses(self):
        m = MessagesWithoutTemperature()
        out = model_call.create_text(Client(m), "m", "sys", "hi")
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
        out = model_call.create_text(Client(m), "m", "sys", "hi")
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


if __name__ == "__main__":
    unittest.main()
