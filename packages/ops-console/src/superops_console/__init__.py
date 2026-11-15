"""superops_console —— HongYun 超级運維智能體（HongYun SuperOps）运维控制台。

对外唯一契约入口：create_app(db_path, token, mode, modelhub_db)。
"""

from .app import create_app
from .auth import load_or_create_token, token_file_path

__version__ = "0.1.0"
__all__ = ["create_app", "load_or_create_token", "token_file_path", "__version__"]
