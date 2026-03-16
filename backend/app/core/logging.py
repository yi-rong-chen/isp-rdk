import logging

LOG_FORMAT = "[%(name)s] [%(asctime)s.%(msecs)03d] [%(levelname)s] %(message)s"
DATE_FORMAT = "%H:%M:%S"


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format=LOG_FORMAT, datefmt=DATE_FORMAT)
