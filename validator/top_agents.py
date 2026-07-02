from typing import Any

import numpy as np


def _get_valid_agents(
    agent_entries: list[dict[str, Any]],
    hotkey_to_uid: dict[str, int],
) -> list[tuple[str, int]]:
    valid_agents = []
    for agent in agent_entries:
        agent_hotkey = agent.get("hotkey")
        if not agent_hotkey:
            continue

        agent_uid = hotkey_to_uid.get(agent_hotkey)
        if agent_uid is not None:
            valid_agents.append((agent_hotkey, agent_uid))

    return valid_agents


def _get_payout_percentages(payout_structure: Any) -> list[float]:
    payout_percentages = []
    has_valid_payout = False

    if isinstance(payout_structure, list) and payout_structure:
        for payout in payout_structure:
            try:
                payout_percentages.append(min(max(float(payout), 0.0), 100.0))
                has_valid_payout = True
            except (TypeError, ValueError):
                payout_percentages.append(0.0)

    if not has_valid_payout:
        return [100.0]

    return payout_percentages


def split_top_agent_scores(
    top_agents_payload: Any,
    metagraph_hotkeys: list[str],
    metagraph_size: int,
) -> tuple[np.ndarray, list[tuple[str, float]], str | None, float]:
    agent_entries = top_agents_payload.get("agents") or []
    burn_entry = top_agents_payload.get("burn") or {}
    payout_percentages = _get_payout_percentages(top_agents_payload.get("payout_structure_pct"))

    new_scores = np.zeros(metagraph_size, dtype=np.float32)
    hotkey_to_uid = {hotkey: uid for uid, hotkey in enumerate(metagraph_hotkeys)}

    burn_hotkey = burn_entry.get("hotkey")
    burn_percentage = burn_entry.get("percentage", 100)
    try:
        burn_fraction = min(max(float(burn_percentage) / 100.0, 0.0), 1.0)
    except (TypeError, ValueError):
        burn_fraction = 0.0

    valid_agents = _get_valid_agents(agent_entries, hotkey_to_uid)

    paid_agent_allocations = []
    post_burn_fraction = 1.0 - burn_fraction
    for index, payout_percentage in enumerate(payout_percentages):
        payout_fraction = post_burn_fraction * (payout_percentage / 100.0)
        if payout_fraction <= 0:
            continue

        if index >= len(valid_agents):
            burn_fraction += payout_fraction
            continue

        agent_hotkey, agent_uid = valid_agents[index]
        new_scores[agent_uid] += payout_fraction
        paid_agent_allocations.append((agent_hotkey, payout_fraction))

    unallocated_payout_percentage = max(100.0 - sum(payout_percentages), 0.0)
    burn_fraction += post_burn_fraction * (unallocated_payout_percentage / 100.0)

    burn_uid = hotkey_to_uid.get(burn_hotkey)
    if burn_uid is not None and burn_fraction > 0:
        new_scores[burn_uid] += burn_fraction

    return new_scores, paid_agent_allocations, burn_hotkey, burn_fraction
