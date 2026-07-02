# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# TODO(developer): Set your name
# Copyright © 2023 <your name>

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import time
import sys
import numpy as np

# Bittensor
import bittensor as bt

from config import settings
from validator.manager import SandboxManager
from validator.top_agents import split_top_agent_scores

# import base validator class which takes care of most of the boilerplate
from template.base.validator import BaseValidatorNeuron

# Bittensor Validator Template:
from template.validator import forward


class Validator(BaseValidatorNeuron):
    """
    Your validator neuron class. You should use this class to define your validator's behavior. In particular, you should replace the forward function with your own logic.

    This class inherits from the BaseValidatorNeuron class, which in turn inherits from BaseNeuron. The BaseNeuron class takes care of routine tasks such as setting up wallet, subtensor, metagraph, logging directory, parsing config, etc. You can override any of the methods in BaseNeuron if you need to customize the behavior.

    This class provides reasonable default behavior for a validator such as keeping a moving average of the scores of the miners and using them to set weights at the end of each epoch. Additionally, the scores are reset for new hotkeys at the end of each epoch.
    """

    def __init__(self, config=None):
        super(Validator, self).__init__(config=config)

        bt.logging.info("load_state()")
        self.load_state()

        # TODO(developer): Anything specific to your use case you can do here
        self.sandbox_manager = SandboxManager(
            is_local=settings.local,
            wallet_name=self.wallet.name,
        )
        bt.logging.info("SandboxManager initialized")

    def update_top_miner_scores(self):
        """
        Fetch the top agents payload from the platform and split scores between
        the burn hotkey and matching ranked agent hotkeys.
        """
        try:
            top_agents = self.sandbox_manager.platform_client.get_top_agents()
        except Exception as e:
            bt.logging.error(f"Failed to fetch top agents: {e}")
            return

        if not top_agents:
            bt.logging.info("No top agents returned from platform")
            return

        new_scores, paid_agent_allocations, burn_hotkey, burn_fraction = split_top_agent_scores(
            top_agents_payload=top_agents,
            metagraph_hotkeys=self.metagraph.hotkeys,
            metagraph_size=self.metagraph.n,
        )

        if not np.any(new_scores):
            bt.logging.warning("No top agent or burn hotkeys found in metagraph")
            return

        self.scores = new_scores
        agent_allocation_log = "\n".join(
            f"rank {index}: {fraction:.2%} -> {hotkey}"
            for index, (hotkey, fraction) in enumerate(paid_agent_allocations, start=1)
        ) or "none"
        bt.logging.info(
            "Updated top miner scores:\n"
            f"agents:\n{agent_allocation_log}\n"
            f"burn:\n{burn_fraction:.2%} -> {burn_hotkey}"
        )

    async def forward(self):
        """
        Validator forward pass. Consists of:
        - Generating the query
        - Querying the miners
        - Getting the responses
        - Rewarding the miners
        - Updating the scores
        """
        # TODO(developer): Rewrite this function based on your protocol definition.
        return await forward(self)

    def check_for_thread_exception(self):
        """
        Check if the background thread has encountered an exception.
        If so, log it, clean up resources, and exit the application.
        """
        if self.thread_exception is not None:
            bt.logging.critical("Validator background thread died with exception:")
            bt.logging.critical(self.thread_exception)

            try:
                if hasattr(self, "dendrite"):
                    bt.logging.info("Closing dendrite session")
                    self.dendrite.close_session()
            except Exception as e:
                bt.logging.error(f"Error closing dendrite session: {e}")

            # Exit with error code so Docker will restart
            bt.logging.critical("Exiting application due to validator thread failure")
            sys.exit(1)


# The main function parses the configuration and runs the validator.
if __name__ == "__main__":
    with Validator() as validator:
        while True:
            time.sleep(5)

            # Check if background thread has died with exception
            validator.check_for_thread_exception()
