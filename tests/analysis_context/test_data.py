from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class DataContextTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / "fixtures/waiting.json").read_text(encoding="utf-8"))

    def test_chain_references_and_event_identity(self):
        chains = {row["chain_id"] for row in self.data["chains"]}
        events = self.data["events"]
        self.assertEqual(len(events), len({row["event_id"] for row in events}))
        self.assertTrue(all(row["chain_id"] in chains for row in events))

    def test_shared_pool_and_unfinished_observations(self):
        self.assertTrue(any(len(row["services"]) > 1 for row in self.data["pools"]))
        completed = {row["chain_id"] for row in self.data["events"] if row["kind"] == "completed" and row["step"] == "consultation"}
        self.assertLess(len(completed), len(self.data["chains"]))
        self.assertEqual(len(self.data["scenarios"]), 3)

