import logging
import os


def get_logger(name: str = "bitsec"):
    """
    Opt into Bittensor logging with USE_BT_LOGGING; otherwise use a plain stdout logger.
    """
    use_bt = os.environ.get("USE_BT_LOGGING", "").lower() in ("1", "true", "yes")
    if use_bt:
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
