# 以下变量由 pipeline/runner.py 从 YAML 配置按用户注入
HOST = ""
CASE_NAME = ""
TRANSLATE_NAME = ""
ANALYSIS = ""

FILES_PER_BATCH = 50

SUPPORTED_EXTENSIONS = {
    ".doc", ".docx", ".ppt", ".pptx", ".pdf", ".rtf", ".txt", ".htm", ".html",
    ".xls", ".xlsx", ".odt", ".hwp", ".hwpx", ".one",
    ".zip", ".rar", ".tar", ".gz", ".tgz", ".7z",
    ".eml", ".msg", ".pst", ".ost",
    ".jpg", ".jpeg", ".png", ".bmp",
}

# 压缩包/归档类型 — 需单独上传，服务端会解压
COMPRESSED_EXTENSIONS = {".zip", ".rar", ".tar", ".gz", ".tgz", ".7z", ".pst", ".ost"}
