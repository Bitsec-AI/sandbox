import unittest

import numpy as np

from validator.top_agents import split_top_agent_scores


class TopAgentsSplitTestCase(unittest.TestCase):
    def assert_allocations(self, allocations, expected_hotkeys, expected_fractions):
        self.assertEqual([hotkey for hotkey, _ in allocations], expected_hotkeys)
        np.testing.assert_allclose(
            np.array([fraction for _, fraction in allocations], dtype=np.float32),
            np.array(expected_fractions, dtype=np.float32),
        )

    def test_split_top_agent_scores_splits_between_agent_and_burn(self):
        scores, paid_agent_allocations, burn_hotkey, burn_fraction = split_top_agent_scores(
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
        self.assert_allocations(paid_agent_allocations, ["agent-hotkey"], [0.75])
        self.assertEqual(burn_hotkey, "burn-hotkey")
        self.assertEqual(burn_fraction, 0.25)

    def test_split_top_agent_scores_honors_full_burn_percentage(self):
        scores, paid_agent_allocations, burn_hotkey, burn_fraction = split_top_agent_scores(
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
        self.assert_allocations(paid_agent_allocations, [], [])
        self.assertEqual(burn_hotkey, "burn-hotkey")
        self.assertEqual(burn_fraction, 1.0)

    def test_split_top_agent_scores_returns_zeroes_when_no_hotkeys_match(self):
        scores, paid_agent_allocations, burn_hotkey, burn_fraction = split_top_agent_scores(
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
        self.assert_allocations(paid_agent_allocations, [], [])
        self.assertEqual(burn_hotkey, "missing-burn")
        self.assertEqual(burn_fraction, 1.0)

    def test_split_top_agent_scores_with_zero_burn_uses_agent_weight(self):
        scores, paid_agent_allocations, burn_hotkey, burn_fraction = split_top_agent_scores(
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
        self.assert_allocations(paid_agent_allocations, ["agent-hotkey"], [1.0])
        self.assertIsNone(burn_hotkey)
        self.assertEqual(burn_fraction, 0.0)

    def test_split_top_agent_scores_applies_payout_structure_to_post_burn_share(self):
        scores, paid_agent_allocations, burn_hotkey, burn_fraction = split_top_agent_scores(
            top_agents_payload={
                "agents": [
                    {"agent_id": 1, "hotkey": "rank-one-hotkey"},
                    {"agent_id": 2, "hotkey": "rank-two-hotkey"},
                    {"agent_id": 3, "hotkey": "rank-three-hotkey"},
                ],
                "burn": {
                    "hotkey": "burn-hotkey",
                    "percentage": 25,
                },
                "payout_structure_pct": [70, 30],
            },
            metagraph_hotkeys=[
                "rank-one-hotkey",
                "rank-two-hotkey",
                "rank-three-hotkey",
                "burn-hotkey",
            ],
            metagraph_size=4,
        )

        np.testing.assert_allclose(
            scores,
            np.array([0.525, 0.225, 0.0, 0.25], dtype=np.float32),
        )
        self.assert_allocations(
            paid_agent_allocations,
            ["rank-one-hotkey", "rank-two-hotkey"],
            [0.525, 0.225],
        )
        self.assertEqual(burn_hotkey, "burn-hotkey")
        self.assertEqual(burn_fraction, 0.25)

    def test_split_top_agent_scores_compacts_payouts_onto_valid_agents(self):
        scores, paid_agent_allocations, burn_hotkey, burn_fraction = split_top_agent_scores(
            top_agents_payload={
                "agents": [
                    {"agent_id": 1, "hotkey": "missing-rank-one"},
                    {"agent_id": 2, "hotkey": "rank-two-hotkey"},
                    {"agent_id": 3, "hotkey": "rank-three-hotkey"},
                ],
                "burn": {
                    "hotkey": "burn-hotkey",
                    "percentage": 25,
                },
                "payout_structure_pct": [70, 30],
            },
            metagraph_hotkeys=["rank-two-hotkey", "rank-three-hotkey", "burn-hotkey"],
            metagraph_size=3,
        )

        np.testing.assert_allclose(
            scores,
            np.array([0.525, 0.225, 0.25], dtype=np.float32),
        )
        self.assert_allocations(
            paid_agent_allocations,
            ["rank-two-hotkey", "rank-three-hotkey"],
            [0.525, 0.225],
        )
        self.assertEqual(burn_hotkey, "burn-hotkey")
        self.assertEqual(burn_fraction, 0.25)

    def test_split_top_agent_scores_adds_extra_payout_entries_to_burn(self):
        scores, paid_agent_allocations, burn_hotkey, burn_fraction = split_top_agent_scores(
            top_agents_payload={
                "agents": [
                    {"agent_id": 1, "hotkey": "agent-hotkey"},
                ],
                "burn": {
                    "hotkey": "burn-hotkey",
                    "percentage": 10,
                },
                "payout_structure_pct": [70, 20, 10],
            },
            metagraph_hotkeys=["agent-hotkey", "burn-hotkey"],
            metagraph_size=2,
        )

        np.testing.assert_allclose(scores, np.array([0.63, 0.37], dtype=np.float32))
        self.assert_allocations(paid_agent_allocations, ["agent-hotkey"], [0.63])
        self.assertEqual(burn_hotkey, "burn-hotkey")
        self.assertAlmostEqual(burn_fraction, 0.37)

    def test_split_top_agent_scores_adds_unallocated_payout_remainder_to_burn(self):
        scores, paid_agent_allocations, burn_hotkey, burn_fraction = split_top_agent_scores(
            top_agents_payload={
                "agents": [
                    {"agent_id": 1, "hotkey": "agent-hotkey"},
                ],
                "burn": {
                    "hotkey": "burn-hotkey",
                    "percentage": 25,
                },
                "payout_structure_pct": [60],
            },
            metagraph_hotkeys=["agent-hotkey", "burn-hotkey"],
            metagraph_size=2,
        )

        np.testing.assert_allclose(scores, np.array([0.45, 0.55], dtype=np.float32))
        self.assert_allocations(paid_agent_allocations, ["agent-hotkey"], [0.45])
        self.assertEqual(burn_hotkey, "burn-hotkey")
        self.assertEqual(burn_fraction, 0.55)


if __name__ == "__main__":
    unittest.main()
