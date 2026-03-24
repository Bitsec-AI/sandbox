from typing import Any

import numpy as np


def split_top_agent_scores(
    top_agents_payload: Any,
    metagraph_hotkeys: list[str],
    metagraph_size: int,
) -> tuple[np.ndarray, str | None, str | None, float]:
    agent_entries = top_agents_payload.get("agents") or []
    burn_entry = top_agents_payload.get("burn") or {}

    new_scores = np.zeros(metagraph_size, dtype=np.float32)

    burn_hotkey = burn_entry.get("hotkey")
    burn_percentage = burn_entry.get("percentage", 100)
    try:
        burn_fraction = min(max(float(burn_percentage) / 100.0, 0.0), 1.0)
    except (TypeError, ValueError):
        burn_fraction = 0.0

    if burn_hotkey and burn_fraction > 0:
        try:
            burn_uid = metagraph_hotkeys.index(burn_hotkey)
            new_scores[burn_uid] += burn_fraction
        except ValueError:
            pass

    selected_agent_hotkey = None
    agent_fraction = 1.0 - burn_fraction
    if agent_fraction > 0:
        for agent in agent_entries:
            agent_hotkey = agent.get("hotkey")
            if not agent_hotkey:
                continue

            try:
                agent_uid = metagraph_hotkeys.index(agent_hotkey)
                new_scores[agent_uid] += agent_fraction
                selected_agent_hotkey = agent_hotkey
                break
            except ValueError:
                continue

    return new_scores, selected_agent_hotkey, burn_hotkey, burn_fraction
