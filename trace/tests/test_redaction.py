import unittest

from trace.capture import redaction


class RedactionTest(unittest.TestCase):
    def test_credit_card_redacted_via_luhn(self):
        # 4111 1111 1111 1111 is a valid Luhn test card.
        out = redaction.redact_text("card 4111 1111 1111 1111 ok")
        self.assertIn("[REDACTED]", out)
        self.assertNotIn("4111", out)

    def test_non_luhn_long_number_kept(self):
        # tracking numbers / order ids are signal, not secrets -> keep them.
        out = redaction.redact_text("tracking 1Z999AA10123456784")
        self.assertIn("1Z999AA10123456784", out)

    def test_ssn_redacted(self):
        self.assertEqual(redaction.redact_text("ssn 123-45-6789"),
                         "ssn [REDACTED]")

    def test_api_key_and_password_assignment(self):
        out = redaction.redact_text("key sk-abcdefabcdefabcdef1234 password=hunter2")
        self.assertNotIn("sk-abcdef", out)
        self.assertNotIn("hunter2", out)

    def test_email_kept(self):
        # recipients are needed downstream; do not redact emails.
        out = redaction.redact_text("to casey@example.com")
        self.assertIn("casey@example.com", out)

    def test_payload_redaction_nested(self):
        p = redaction.redact_payload("structured_read", {
            "anchor": "click",
            "elements": [{"role": "textbox", "label": "SSN", "value": "123-45-6789"}],
        })
        self.assertEqual(p["elements"][0]["value"], "[REDACTED]")

    def test_clipboard_content_redacted(self):
        p = redaction.redact_payload("clipboard", {"op": "copy", "content": "4111 1111 1111 1111"})
        self.assertEqual(p["content"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
