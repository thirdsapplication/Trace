"""Every engine must be able to build its own model caller.

`_default_model_fn` is the path taken in production and NEVER in the suite: every
test injects `model_fn`, so the default builder is dead code as far as the tests are
concerned. That let a missing import ship — `model_call` was referenced inside
configgen's builder and never imported, which Python does not notice until the
function runs. It surfaced when a user pressed Build:

    RuntimeError: couldn't prepare questions for: B1
    (NameError: name 'model_call' is not defined)

Importing a module cannot catch this, because the name is only looked up at call
time. So this checks the names those builders depend on are actually bound in each
module — the cheapest thing that would have caught it.
"""

import importlib
import unittest

# module path -> names its _default_model_fn (or equivalent) depends on at call time
ENGINES = {
    "trace.agent.configgen": ["model_call"],
    "trace.agent.learn": ["model_call"],
    "trace.discovery.discover": ["model_call"],
    "trace.discovery.scorer": ["model_call"],
    "trace.reconstruct.reconstruct": ["model_call"],
    "trace.artifact.artifact": ["model_call"],
}


class EngineImportTest(unittest.TestCase):
    def test_every_engine_resolves_what_its_builder_calls(self):
        missing = []
        for mod, names in ENGINES.items():
            m = importlib.import_module(mod)
            for n in names:
                if not hasattr(m, n):
                    missing.append(f"{mod}.{n}")
        self.assertEqual(missing, [],
                         f"referenced but never imported: {missing}. These raise "
                         "NameError the first time a real model call is made, which "
                         "no test does.")

    def test_the_shared_caller_is_importable_on_its_own(self):
        m = importlib.import_module("trace.agent.model_call")
        self.assertTrue(callable(m.create_text))

    def test_the_check_would_notice_a_missing_name(self):
        """A guard that cannot fail is not a guard."""
        m = importlib.import_module("trace.agent.configgen")
        self.assertFalse(hasattr(m, "definitely_not_imported_anywhere"))


if __name__ == "__main__":
    unittest.main()
