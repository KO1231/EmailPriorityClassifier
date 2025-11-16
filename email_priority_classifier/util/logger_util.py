import logging
import os
import sys
from logging import handlers
from pathlib import Path

_FORMAT = '%(levelname)s %(asctime)s [%(name)s - %(funcName)s] %(message)s'
_HANDLER = logging.StreamHandler(sys.stdout)
_HANDLER.setLevel(logging.DEBUG)
_HANDLER.setFormatter(logging.Formatter(_FORMAT))
_HANDLER.addFilter(lambda record: record.levelno < logging.ERROR)

_ERROR_HANDLER = logging.StreamHandler(sys.stderr)
_ERROR_HANDLER.setLevel(logging.ERROR)
_ERROR_HANDLER.setFormatter(logging.Formatter(_FORMAT))

_LOG_FILE = os.environ.get("EMAIL_PRIORITY_CLASSIFIER_LOG", Path(__file__).parent.parent.parent / "log" / "application.log")
_LOG_FILE.parent.mkdir(exist_ok=True)
_LOG_FILE_HANDLER = handlers.RotatingFileHandler(
    _LOG_FILE,
    encoding='utf-8',
    maxBytes=1 * (2 ** 20),  # 1 MB
    backupCount=5
)
_LOG_FILE_HANDLER.setLevel(logging.DEBUG)
_LOG_FILE_HANDLER.setFormatter(logging.Formatter(_FORMAT))


def setup_logger(name: str, level: int = logging.DEBUG):
    logger: logging.Logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.addHandler(_HANDLER)
    logger.addHandler(_ERROR_HANDLER)
    logger.addHandler(_LOG_FILE_HANDLER)
    return logger
