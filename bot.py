import os
from pathlib import Path

# 显式加载项目根目录的 .env 到 os.environ（NoneBot2 的 dotenv 不保证注入 os.environ）
_env = Path(__file__).parent / ".env"
if _env.is_file():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v)

import nonebot
from nonebot.adapters.onebot.v11 import Adapter

nonebot.init()
driver = nonebot.get_driver()
driver.register_adapter(Adapter)
nonebot.load_plugin("plugins.hermes_bridge")
nonebot.run()
