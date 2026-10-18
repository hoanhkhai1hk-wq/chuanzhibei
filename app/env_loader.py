"""
《体检之后》—— 本地 .env 读取

================================================================================
为什么要有这一层
================================================================================
业务代码读配置只认 ``os.getenv(...)``，这是对的：环境变量是最通用的配置入口，
换部署方式也不用改业务代码。但 ``os.getenv`` 读不到磁盘上的文件，
于是就出现了一个尴尬的错位——

    .env.example   写着「把 Key 填到这里」（它是**进 Git 的**模板）
    .env           真正的本地配置（**.gitignore 已排除**）

如果项目不主动去读 ``.env``，使用者就只能二选一：
要么把真实 Key 写进被 Git 跟踪的 ``.env.example``（一提交就泄露），
要么每次开终端都手工 set 一遍环境变量（双击启动.bat 的人做不到）。

这一层就是把 ``.env`` 里的键值灌进 ``os.environ``，让 ``os.getenv`` 照常工作，
业务代码一行都不用改。

================================================================================
三条硬规矩
================================================================================
1. **只补不盖**：真实环境变量优先。``.env`` 只填补空缺，
   绝不用文件里的值覆盖 shell / 系统里已经设好的值。
   否则线上配好了 Key，本地一个过期 .env 反而把它顶掉了。

2. **只跑一次**：幂等。进程内第一次调用生效，之后无论谁再调都直接返回，
   不会有"两个模块各加载一遍、互相打架"的情况。

3. **绝不报错**：文件不存在、权限不足、编码异常，统统安静返回。
   没有 .env 是合法的（模板模式照样能跑），不该让配置缺失变成启动崩溃。

================================================================================
测试为什么能不受影响
================================================================================
测试里有大量「无 Key → 走模板模式」的断言。如果 .env 被加载，
``Explainer(api_key="")`` 会因为 fallback 到 ``os.getenv`` 而突然变成 LLM 模式，
这些断言全部失效——而它们正是"架构铁律"的守门人。

所以提供 :func:`mark_loaded`：在导入任何业务模块**之前**调一次，
把加载器标记为"已处理"，后续的自动加载就会跳过。
``tests/conftest.py`` 就是这么做的。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

#: 项目根：本文件位于 <root>/app/env_loader.py
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 默认读取的文件名
ENV_FILE = PROJECT_ROOT / ".env"

# 幂等标志。None = 还没处理过；Path = 已加载该文件；False = 标记为已处理（但未加载）
_state: Optional[Path | bool] = None


def _parse_line(line: str) -> Optional[tuple[str, str]]:
    """把一行文本解析成 (键, 值)。不是合法的 KEY=VALUE 就返回 None。

    容忍这些写法：前后空格、``export`` 前缀、单引号/双引号包裹、行尾注释前的值。
    不追求完整 shell 语法——配置文件是人手写的，别让人去猜规则。
    """
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    if text.startswith("export "):
        text = text[len("export "):].strip()
    if "=" not in text:
        return None

    key, _, raw = text.partition("=")
    key = key.strip()
    if not key:
        return None

    value = raw.strip()
    # 去成对引号。不成对时原样保留，避免把 "abc 当成 abc 而静默吞掉意图
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return key, value


def _read(path: Path) -> dict[str, str]:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    # BOM 会让第一行的键变成 "\ufeffDEEPSEEK_API_KEY"，静默失效，必须剥掉
    if content.startswith("\ufeff"):
        content = content[1:]

    pairs: dict[str, str] = {}
    for line in content.splitlines():
        got = _parse_line(line)
        if got:
            pairs[got[0]] = got[1]
    return pairs


def load_dotenv_once(env_file: Path | None = None, *, override: bool = False) -> Optional[Path]:
    """把 .env 灌进 os.environ。只跑一次，只补不盖，失败也不抛。

    :param env_file: 指定文件；None 表示用默认的 :data:`ENV_FILE`
    :param override: True 时用文件里的值覆盖已有环境变量（一般不要开）
    :return: 实际加载的文件路径；已处理过或文件不存在则返回 None
    """
    global _state
    if _state is not None:
        return _state if isinstance(_state, Path) else None

    path = Path(env_file) if env_file is not None else ENV_FILE
    if not path.is_file():
        _state = False
        return None

    loaded = 0
    for key, value in _read(path).items():
        if override or key not in os.environ:
            os.environ[key] = value
            loaded += 1

    _state = path if loaded else False
    return path if loaded else None


def mark_loaded() -> None:
    """标记为"已处理"，阻止后续自动加载。

    给测试用：确测试环境是确定性的，不受开发者本机 .env 影响。
    """
    global _state
    _state = False


def loaded_path() -> Optional[Path]:
    """返回本次进程实际加载的 .env 路径，没加载则返回 None。供自检与日志用。"""
    return _state if isinstance(_state, Path) else None


def status_line() -> str:
    """一行人类可读的状态，给启动日志与排查用。**不含密钥本身**。"""
    path = loaded_path()
    if path is None:
        return ".env: 未加载（文件不存在或已被跳过）"
    keys = ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "MOONSHOT_API_KEY")
    hit = [k for k in keys if os.getenv(k, "")]
    tail = f"；已配置 {len(hit)} 个模型 Key" if hit else "；未配置任何模型 Key"
    return f".env: 已加载 {path.name}{tail}"
