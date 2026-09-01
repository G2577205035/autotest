import logging

# ANSI 颜色 — 只着色级别标签
_RESET = "\033[0m"
_LEVEL_COLORS = {
    "DEBUG": "\033[90m",     # 灰色
    "INFO": "\033[32m",      # 绿色
    "WARNING": "\033[33m",   # 黄色
    "ERROR": "\033[31m",     # 红色
    "CRITICAL": "\033[35m",  # 紫色
}


class _ColoredFormatter(logging.Formatter):
    def format(self, record):
        c = _LEVEL_COLORS.get(record.levelname, "")
        record.levelname = f"{c}{record.levelname}{_RESET}"
        return super().format(record)


handler = logging.StreamHandler()
handler.setFormatter(_ColoredFormatter(
    "%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
))

log = logging.getLogger("liema")
log.setLevel(logging.INFO)
log.handlers = [handler]
log.propagate = False
