import logging


def get_logger(name: str = "bitsec"):
    """
    Use Bittensor's logger when available to avoid duplicate handlers.
    Falls back to a standard python logger for standalone runs.
    """
    try:
        import bittensor as bt

        return bt.logging
    except Exception:
        pass

    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

    return logger
