"""插件改名的一次性迁移：``astrbot_plugin_user_gateway`` → ``astrbot_plugin_moe_permission``。

AstrBot 用「目录名 / 插件名」当插件身份，改名后等于一个全新插件：

- 数据目录 ``data/plugin_data/<插件名>/`` 里的 ``user_gateway.db``（权限规则、等级、
  归级、额度、用量、好友群缓存、最后回复）、``avatars/`` 头像缓存、``fonts/`` 投放字体
  会被孤立；
- 配置文件 ``data/config/<插件名>_config.json`` 会重新按 schema 默认值生成。

本模块在 ``initialize()`` 最早期做一次性搬迁，**旧文件一律保留不动**（便于回退，
也便于之后卸载旧插件）。刻意不 import ``astrbot``：纯标准库，可以脱离 AstrBot 环境
直接跑 ``tests/test_rename_migration.py``。

与 ``moe_box`` 那边的做法有一处必须不同：它用 ``shutil.copytree`` 且判据是
「新目录不存在」。本插件走 ``StarTools.get_data_dir()``，核心会**自动创建**新目录，
所以那个判据永远为假。这里改成逐文件搬迁、只在目标文件不存在时复制 —— 既不覆盖
改名后新产生的数据，又能重入（第二次跑自然一个文件都不搬）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Callable

OLD_PLUGIN_NAME = "astrbot_plugin_user_gateway"
NEW_PLUGIN_NAME = "astrbot_plugin_moe_permission"

# 库文件名**保持不变**：旧目录整份搬过来就是这个名字，改了反而找不到数据。
DB_FILE_NAME = "user_gateway.db"
OLD_CONFIG_FILE = f"{OLD_PLUGIN_NAME}_config.json"

LogFn = Callable[[str], None]


def _noop(_msg: str) -> None:
    """默认日志回调：静默丢弃（自检脚本不必伪造 astrbot logger）。"""


def migrate_data_dir(data_dir: Path | str, log: LogFn = _noop) -> int:
    """把旧插件数据目录的内容逐个搬到新目录，返回搬了几个文件。

    只复制「新目录里还没有」的文件，因此：

    - 新插件已经跑过一阵、产生了新数据时，绝不会被旧数据覆盖；
    - 搬迁过程被中断后重跑是安全的（已搬过的跳过，没搬的继续）；
    - 旧目录保持原样，用户随时能回退到旧插件。
    """
    target = Path(data_dir)
    old_dir = target.parent / OLD_PLUGIN_NAME
    if old_dir == target or not old_dir.is_dir():
        return 0

    try:
        src_files = sorted(p for p in old_dir.rglob("*") if p.is_file())
    except OSError as e:
        log(f"插件改名迁移：旧数据目录遍历失败（跳过）: {e}")
        return 0

    moved = 0
    for src in src_files:
        dst = target / src.relative_to(old_dir)
        if dst.exists():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            moved += 1
        except OSError as e:
            log(f"插件改名迁移：搬运 {src.name} 失败（跳过）: {e}")

    if moved:
        log(
            f"插件改名迁移：数据目录 {old_dir} → {target}（{moved} 个文件，旧目录保留）"
        )
    return moved


def migrate_config(config: Any, log: LogFn = _noop) -> int:
    """仅在 AstrBot 这次刚创建新配置时，把旧配置整体搬进来，返回搬了几个键。

    ``AstrBotConfig`` 只有在配置文件原本不存在（按 schema 默认值新建）时才会有
    ``first_deploy=True``。这正是唯一安全的搬迁时机：此后每次启动该属性都不存在，
    不会把用户后来改过的配置覆盖回旧值。
    """
    config_path = str(getattr(config, "config_path", "") or "")
    if not config_path:
        return 0
    old_cfg = Path(config_path).parent / OLD_CONFIG_FILE
    if not old_cfg.is_file() or not bool(getattr(config, "first_deploy", False)):
        return 0

    try:
        old_values = json.loads(old_cfg.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        log(f"插件改名迁移：旧配置读取失败（沿用默认配置）: {e}")
        return 0
    if not isinstance(old_values, dict) or not old_values:
        return 0

    try:
        config.update(old_values)
        config.save_config()
    except Exception as e:  # save_config 可能抛各种底层 IO 错误，一律降级不打断启动
        log(f"插件改名迁移：旧配置写回失败（沿用默认配置）: {e}")
        return 0

    log(f"插件改名迁移：已从 {old_cfg.name} 搬入 {len(old_values)} 个配置键")
    return len(old_values)


def migrate_renamed_plugin(
    data_dir: Path | str, config: Any, log: LogFn = _noop
) -> tuple[int, int]:
    """入口：返回 ``(搬迁文件数, 搬迁配置键数)``。

    调用方必须保证这一步在**打开数据库、读取配置之前**执行。
    """
    return migrate_data_dir(data_dir, log), migrate_config(config, log)
