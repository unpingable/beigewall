import json
import unittest

from beigewall.agent.drivers import LLMDriver


class TestNarrativeParsing(unittest.TestCase):
    def test_fenced_json_is_parsed(self):
        fenced = "```json\n{\"claims\": [{\"resource\": \"r\", \"objects\": [\"o\"],"
        fenced += " \"fields\": [\"f\"]}], \"declared_effects\": [],"
        fenced += " \"summary\": \"s\"}\n```"
        out = LLMDriver._parse_narrative(fenced)
        self.assertEqual(out["claims"][0]["objects"], ["o"])
        self.assertEqual(out["summary"], "s")

    def test_bare_json_still_parsed(self):
        out = LLMDriver._parse_narrative('{"claims": [], "declared_effects": ["send_message"], "summary": "x"}')
        self.assertEqual(out["declared_effects"], ["send_message"])

    def test_unparseable_falls_back_to_raw_summary(self):
        out = LLMDriver._parse_narrative("I read some stuff, honestly.")
        self.assertEqual(out["claims"], [])
        self.assertEqual(out["summary"], "I read some stuff, honestly.")

    def test_fenced_without_language_tag(self):
        out = LLMDriver._parse_narrative('```\n{"claims": [], "declared_effects": [], "summary": "s"}\n```')
        self.assertEqual(out["summary"], "s")


if __name__ == "__main__":
    unittest.main()
