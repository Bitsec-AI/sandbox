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


class PrefixedLogger:
    def __init__(self, logger, prefix):
        self.logger = logger
        self.prefix = prefix

    def debug(self, msg, *args, **kwargs):
        return self.logger.debug(f"{self.prefix}{msg}", *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        return self.logger.info(f"{self.prefix}{msg}", *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        return self.logger.error(f"{self.prefix}{msg}", *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        return self.logger.warning(f"{self.prefix}{msg}", *args, **kwargs)

    def exception(self, msg, *args, **kwargs):
        return self.logger.exception(f"{self.prefix}{msg}", *args, **kwargs)

    def __getattr__(self, name):
        # Forward ALL other attributes/methods to underlying logger
        return getattr(self.logger, name)
