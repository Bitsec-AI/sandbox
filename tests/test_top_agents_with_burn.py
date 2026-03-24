import unittest

import numpy as np

from validator.top_agents import split_top_agent_scores


class TopAgentsSplitTestCase(unittest.TestCase):
    def test_split_top_agent_scores_splits_between_agent_and_burn(self):
        scores, selected_agent_hotkey, burn_hotkey, burn_fraction = split_top_agent_scores(
            top_agents_payload={
                "agents": [
                    {"agent_id": 1, "hotkey": "not-in-metagraph"},
                    {"agent_id": 2, "hotkey": "agent-hotkey"},
                ],
                "burn": {
                    "hotkey": "burn-hotkey",
                    "percentage": 25,
                },
            },
            metagraph_hotkeys=["other-hotkey", "agent-hotkey", "burn-hotkey"],
            metagraph_size=3,
        )

        np.testing.assert_allclose(scores, np.array([0.0, 0.75, 0.25], dtype=np.float32))
        self.assertEqual(selected_agent_hotkey, "agent-hotkey")
        self.assertEqual(burn_hotkey, "burn-hotkey")
        self.assertEqual(burn_fraction, 0.25)

    def test_split_top_agent_scores_honors_full_burn_percentage(self):
        scores, selected_agent_hotkey, burn_hotkey, burn_fraction = split_top_agent_scores(
            top_agents_payload={
                "agents": [
                    {"agent_id": 1, "hotkey": "agent-hotkey"},
                ],
                "burn": {
                    "hotkey": "burn-hotkey",
                    "percentage": 100,
                },
            },
            metagraph_hotkeys=["agent-hotkey", "burn-hotkey"],
            metagraph_size=2,
        )

        np.testing.assert_allclose(scores, np.array([0.0, 1.0], dtype=np.float32))
        self.assertIsNone(selected_agent_hotkey)
        self.assertEqual(burn_hotkey, "burn-hotkey")
        self.assertEqual(burn_fraction, 1.0)

    def test_split_top_agent_scores_returns_zeroes_when_no_hotkeys_match(self):
        scores, selected_agent_hotkey, burn_hotkey, burn_fraction = split_top_agent_scores(
            top_agents_payload={
                "agents": [
                    {"agent_id": 1, "hotkey": "missing-agent"},
                ],
                "burn": {
                    "hotkey": "missing-burn",
                    "percentage": 50,
                },
            },
            metagraph_hotkeys=["agent-hotkey", "burn-hotkey"],
            metagraph_size=2,
        )

        np.testing.assert_allclose(scores, np.array([0.0, 0.0], dtype=np.float32))
        self.assertIsNone(selected_agent_hotkey)
        self.assertEqual(burn_hotkey, "missing-burn")
        self.assertEqual(burn_fraction, 0.5)

    def test_split_top_agent_scores_with_zero_burn_uses_agent_weight(self):
        scores, selected_agent_hotkey, burn_hotkey, burn_fraction = split_top_agent_scores(
            top_agents_payload={
                "agents": [
                    {"agent_id": 1, "hotkey": "agent-hotkey"},
                ],
                "burn": {
                    "percentage": 0,
                },
            },
            metagraph_hotkeys=["agent-hotkey", "burn-hotkey"],
            metagraph_size=2,
        )

        np.testing.assert_allclose(scores, np.array([1.0, 0.0], dtype=np.float32))
        self.assertEqual(selected_agent_hotkey, "agent-hotkey")
        self.assertIsNone(burn_hotkey)
        self.assertEqual(burn_fraction, 0.0)


if __name__ == "__main__":
    unittest.main()
