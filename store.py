"""SQLite 存储层：权限规则、等级、token 额度、用量、好友/群缓存、最后消息。

设计要点（详见 docs/astrbot_plugin_moe_permission_PRD.md §7）：

- 单文件库 ``<插件数据目录>/user_gateway.db``，WAL 模式 + ``synchronous=NORMAL``，
  满足「统计写入频繁 + 控制台随时查询」的并发读写。
- 时间统一用 **epoch 秒**（INTEGER），聚合按 SQLite 的 ``localtime`` 分桶，
  避免 Python 侧时区换算带来的边界问题。
- ``usage_log`` 是统计与明细的唯一事实来源：被拒绝的请求也写一条（``status='denied'``），
  只有真正成功/失败的 LLM 调用写 token 数。
- **额度与用量分离**（v2）：``llm_quota`` 只存「限额规则」（可能是某个对象专属，也可能是
  全局/某等级这类模板），``usage_counter`` 存「每个对象每个周期的已用量」。
  两者分离才能在引入等级后做到「额度模板换一个，既有用量不清零」。
- 额度解析优先级（最具体的一层生效）：
  ``user 专属 → 用户等级 → group 专属 → 群等级 → global 全局``。
- 所有写操作都是幂等 upsert，冷启动与并发同步都安全。
"""

from __future__ import annotations

import csv
import io
import json
import time
from typing import Any, Iterable, Mapping, Optional

try:
    import aiosqlite
except ImportError as e:  # pragma: no cover - AstrBot 启动时按 requirements 安装
    raise ImportError(
        "astrbot_plugin_moe_permission 需要 aiosqlite，请先安装：pip install aiosqlite",
    ) from e


SCHEMA_VERSION = 11

# policy 表的 feature 维度补充：
#   model        → 专属模型（effect 存提供商 id；'' / 不存在 = 跟随等级配置）。
#                  v1.3.12 起**私聊（user 层）与群聊（group 层）共用这一个 feature**：
#                  user = 好友专属（仅私聊生效）、group = 群专属（对全群生效），
#                  /切换模型 改的也是它（私聊切自己的、群里管理员切这个群的）。
#   model_choice → 【已废弃】旧版「用户自己切换的模型」（按场景分开存）。
#                  它按**用户**存群场景的选择，导致「在 A 群切换 → 这个人在所有群都换了」
#                  （跨群串号）。启动时由 migrate_model_choice 迁到 model 后清空；
#                  常量与导入白名单保留，是为了旧备份文件还能原样导入（导入后同样闲置）。
MODEL_FEATURE = "model"
MODEL_CHOICE_FEATURE = "model_choice"

# policy 表里「effect 存的是提供商 id 而不是 allow/deny」的 feature 集合。
# 导入 / 读取时要区别对待：这些行不能过 _norm_effect（否则 provider id 会被抹成 inherit）。
PROVIDER_FEATURES: tuple[str, ...] = (MODEL_FEATURE, MODEL_CHOICE_FEATURE)

# set_user_level_scene 的哨兵：UNSET = 不动该场景（区别于 None = 取消）
_LEVEL_UNSET = object()

# 「群成员」规则的 scope_id 约定：``"<群号>:<QQ>"``。
# 权限模型里「某人在某个群」是一个独立维度（比「好友专属（群聊）」更具体），
# 用扁平的复合键最省事，不需要给 policy 再加列。
MEMBER_SCOPE_SEP = ":"


def member_scope_id(group_id: Any, user_id: Any) -> str:
    """拼出群成员规则的 ``scope_id``（``群号:QQ``）。"""
    return f"{str(group_id or '').strip()}{MEMBER_SCOPE_SEP}{str(user_id or '').strip()}"


def parse_provider_list(raw: Any) -> list[str]:
    """把「等级的可用模型列表」解析成提供商 id 列表（去空、去重、保序）。

    库里存的是 JSON 文本（如 ``["pid_a","pid_b"]``，空列表存空串）；同时兼容：
    直接传 list（API / 导入路径）、以及手工填进去的一串裸 id（JSON 解析失败时按单个 id 处理）。
    """
    if isinstance(raw, (list, tuple)):
        items: list[Any] = list(raw)
    else:
        text = str(raw or "").strip()
        if not text:
            return []
        try:
            got = json.loads(text)
        except Exception:
            return [text]
        items = list(got) if isinstance(got, (list, tuple)) else [got]
    out: list[str] = []
    for it in items:
        pid = str(it or "").strip()
        if pid and pid not in out:
            out.append(pid)
    return out


def dump_provider_list(value: Any) -> str:
    """把提供商 id 列表规整成存库用的 JSON 文本（空列表存空串，保持旧库的紧凑形态）。"""
    items = parse_provider_list(value)
    return json.dumps(items, ensure_ascii=False) if items else ""


def _as_list(value: Any) -> list[Any]:
    """把导入的字段当列表用；不是列表就返回空。

    导入是「尽量恢复」：一条坏数据不该让整次导入炸掉，所以这里与 :func:`_norm_effect`
    都做宽松处理，坏行由调用方计入 ``skipped``。
    """
    return list(value) if isinstance(value, (list, tuple)) else []


def _norm_effect(value: Any) -> str:
    """把 effect 规整成 ``allow`` / ``deny`` / ``inherit``（未知值一律当 ``inherit``）。"""
    got = str(value or "").strip().lower()
    return got if got in ("allow", "deny", "inherit") else "inherit"


def _as_int(value: Any, default: int = 0) -> int:
    """宽松转 int（导入的数据可能有 ``null`` / 字符串 / 脏值）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_flag(value: Any) -> int:
    """把布尔/字符串/数字规整成 SQLite 的 0/1。

    入参可能是 JSON 里的 ``true``、``"1"``、``"on"``、``1``（导入路径什么都可能来），
    统一收口在这里，避免把 ``"false"`` 这类真值字符串写进 INTEGER 列。
    """
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if int(value) else 0
    return 1 if str(value or "").strip().lower() in ("1", "true", "yes", "on", "是", "开") else 0


def parse_member_scope_id(scope_id: Any) -> tuple[str, str]:
    """拆开群成员规则的 ``scope_id`` → ``(group_id, user_id)``；格式不对返回两个空串。"""
    text = str(scope_id or "")
    group_id, _, user_id = text.partition(MEMBER_SCOPE_SEP)
    return (group_id.strip(), user_id.strip())

# 权限规则的「场景」维度：
#   ANY（''）  → 两个场景都生效（旧数据、以及群 / 全局这类本来就不分场景的规则）
#   PRIVATE    → 只在私聊生效
#   GROUP      → 只在群聊生效
# 判定时的回落顺序是「场景专属 → 通用」，所以历史数据的行为不会因为引入场景而改变。
SCENE_ANY = ""
SCENE_PRIVATE = "private"
SCENE_GROUP = "group"
SCENES: tuple[str, ...] = (SCENE_ANY, SCENE_PRIVATE, SCENE_GROUP)

# 额度周期：越靠前越"紧凑"，超限时优先报告它
PERIODS: tuple[str, ...] = ("day", "month", "total")

# policy 表的 feature 维度：
#   llm              → LLM 对话权限
#   command          → **对象级指令总权限**（M5：这个好友/群/等级能不能用指令）
#   command:<指令名>  → 某条指令的权限（指令名取 AstrBot 注册表里的完整主名，
#                      形如 "帮助" / "群管 禁言"，别名不单独建键）
# 注意 `command` 与 `command:%` 前缀不重叠，所以两类规则互不干扰。
LLM_FEATURE = "llm"
COMMAND_MASTER_FEATURE = "command"
COMMAND_FEATURE_PREFIX = "command:"


def command_feature(command: str) -> str:
    """指令权限在 ``policy.feature`` 里的键。"""
    return f"{COMMAND_FEATURE_PREFIX}{str(command or '').strip()}"


def command_of_feature(feature: str) -> str:
    """从 feature 键反解指令名（不是指令 feature 时返回空串）。"""
    text = str(feature or "")
    return text[len(COMMAND_FEATURE_PREFIX) :] if text.startswith(COMMAND_FEATURE_PREFIX) else ""

SCHEMA_SQL = """
-- 插件内部 KV（schema_version、上次同步时间等）
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  INTEGER NOT NULL
);

-- 好友缓存（来自协议端 get_friend_list）
CREATE TABLE IF NOT EXISTS friend_cache (
    platform_id TEXT    NOT NULL,
    uin         TEXT    NOT NULL,
    nickname    TEXT    NOT NULL DEFAULT '',
    remark      TEXT    NOT NULL DEFAULT '',
    is_friend   INTEGER NOT NULL DEFAULT 1,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (platform_id, uin)
);

-- 群缓存（来自协议端 get_group_list）
CREATE TABLE IF NOT EXISTS group_cache (
    platform_id      TEXT    NOT NULL,
    group_id         TEXT    NOT NULL,
    name             TEXT    NOT NULL DEFAULT '',
    member_count     INTEGER NOT NULL DEFAULT 0,
    max_member_count INTEGER NOT NULL DEFAULT 0,
    owner            TEXT    NOT NULL DEFAULT '',
    updated_at       INTEGER NOT NULL,
    PRIMARY KEY (platform_id, group_id)
);

-- 群成员缓存（v7）：从协议端 get_group_member_list 拉取，用于「群成员级管控」。
-- 只缓存成员身份信息；权限规则仍在 policy 表（scope_type='member'）。
CREATE TABLE IF NOT EXISTS group_member (
    platform_id TEXT    NOT NULL DEFAULT '',
    group_id    TEXT    NOT NULL,
    user_id     TEXT    NOT NULL,
    nickname    TEXT    NOT NULL DEFAULT '',
    card        TEXT    NOT NULL DEFAULT '',
    role        TEXT    NOT NULL DEFAULT '',
    level       TEXT    NOT NULL DEFAULT '',
    joined_at   INTEGER NOT NULL DEFAULT 0,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (platform_id, group_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_gm_group ON group_member(group_id, role);

-- 权限规则
-- feature: 'llm' | 'command'（对象级指令总权限） | 'command:<指令名>'
-- scene  : ''（通用，两个场景都生效；旧数据都是这个） | 'private' | 'group'
--          —— 用来把「好友/好友等级」这一层的规则按会话类型拆开：
--             私聊禁用某人，不影响他在群里继续用。
-- scope_type='member' 时 scope_id 是 ``"<群号>:<QQ>"``（见 member_scope_id()），
--         表示「这个人在这个群里」的规则，比群规则更具体。
CREATE TABLE IF NOT EXISTS policy (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type  TEXT    NOT NULL,
    scope_id    TEXT    NOT NULL,
    feature     TEXT    NOT NULL DEFAULT 'llm',
    scene       TEXT    NOT NULL DEFAULT '',
    effect      TEXT    NOT NULL,
    note        TEXT    NOT NULL DEFAULT '',
    updated_at  INTEGER NOT NULL,
    UNIQUE (scope_type, scope_id, feature, scene)
);
CREATE INDEX IF NOT EXISTS idx_policy_scope ON policy(scope_type, scope_id);

-- token 限额规则（v2 起只存「上限」，已用量在 usage_counter）
-- scope_type: user | group（某对象专属） / level（某等级模板） / global（全局模板，scope_id 固定 '*'）
CREATE TABLE IF NOT EXISTS llm_quota (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type   TEXT    NOT NULL,
    scope_id     TEXT    NOT NULL,
    period       TEXT    NOT NULL,
    limit_tokens INTEGER NOT NULL,
    mode         TEXT    NOT NULL DEFAULT 'enforce',
    reset_at     INTEGER,
    updated_at   INTEGER NOT NULL,
    UNIQUE (scope_type, scope_id, period)
);
CREATE INDEX IF NOT EXISTS idx_quota_scope ON llm_quota(scope_type, scope_id);

-- 自定义等级：私聊与群聊各一套（kind 区分），等级本身可带默认权限与模型路由
-- effect         → 默认 **LLM** 权限（inherit | allow | deny）
-- command_effect → 默认 **指令** 权限（M5；inherit | allow | deny，deny 即该等级不能用任何指令）
-- *_group        → **群聊场景**下的那一套（v6）。只对 kind='user'（好友等级）有意义：
--                  好友等级在群里也生效，所以要能单独说「私聊禁止但群里照用」。
--                  `inherit` = 跟随主值（= 私聊那套），这就是旧数据的语义，行为不变。
-- kind='group'（群聊等级）只在群里出现，用主值即可，*_group 保持 inherit。
-- provider_id / fallback_provider_id 存 AstrBot 的**提供商 id**（在 AstrBot「模型提供商」里配置的那个）。
-- 模型选择以「提供商」为单位：AstrBot 里一个提供商就对应一个模型，
-- 再单独存一个模型名只会让配置变含混（v0.4.0 的教训），故不设该列。
-- switch_providers（v9）→ 该等级**允许用户自助切换**的提供商 id 列表（JSON 文本，空 = 用兜底三项）。
-- 用户发 /切换模型 时只能在这份名单（外加他自己的专属模型）里挑，
-- 这样「让人自己选模型」不会变成「谁都能挑最贵的那个」。
-- switch_enabled（v10）→ 该等级**是否允许切换**（0 = 只读，只能看不能切；**默认关**）。
--   与名单分开两列是有意的：先「开不开」再「能挑哪些」；
--   只读时名单仍然要展示（用户得知道自己现在用的是哪个），所以不能只靠名单空不空来表达。
-- detect_enabled（v11）→ 该等级**是否允许用 `/切换模型检测`**（0 = 不允许；**默认关**）。
--   它花的是真额度（检测要真打一次模型），所以与「能不能切换」分开两个开关：
--   「允许看看有哪些模型」和「允许花钱去打一遍」不是一回事。
CREATE TABLE IF NOT EXISTS quota_level (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                 TEXT    NOT NULL,
    name                 TEXT    NOT NULL,
    description          TEXT    NOT NULL DEFAULT '',
    effect               TEXT    NOT NULL DEFAULT 'inherit',
    command_effect       TEXT    NOT NULL DEFAULT 'inherit',
    effect_group         TEXT    NOT NULL DEFAULT 'inherit',
    command_effect_group TEXT    NOT NULL DEFAULT 'inherit',
    sort_order           INTEGER NOT NULL DEFAULT 0,
    provider_id          TEXT    NOT NULL DEFAULT '',
    fallback_provider_id TEXT    NOT NULL DEFAULT '',
    switch_providers     TEXT    NOT NULL DEFAULT '',
    switch_enabled       INTEGER NOT NULL DEFAULT 0,
    detect_enabled       INTEGER NOT NULL DEFAULT 0,
    updated_at           INTEGER NOT NULL,
    UNIQUE (kind, name)
);

-- 对象归级：一个好友/群最多属于一个等级
-- v8 起好友的等级**分场景**：level_id = 私聊（同时是群聊的默认），
-- level_id_group = 群聊专属（NULL = 跟随私聊等级）；群对象只用 level_id。
CREATE TABLE IF NOT EXISTS subject_level (
    scope_type     TEXT    NOT NULL,
    scope_id       TEXT    NOT NULL,
    level_id       INTEGER,
    level_id_group INTEGER,
    updated_at     INTEGER NOT NULL,
    PRIMARY KEY (scope_type, scope_id)
);
CREATE INDEX IF NOT EXISTS idx_subject_level ON subject_level(level_id);

-- 用量计数：每个对象每个周期的已用量（与「是否配了额度」无关，始终累加）
CREATE TABLE IF NOT EXISTS usage_counter (
    scope_type  TEXT    NOT NULL,
    scope_id    TEXT    NOT NULL,
    period      TEXT    NOT NULL,
    used_tokens INTEGER NOT NULL DEFAULT 0,
    reset_at    INTEGER,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (scope_type, scope_id, period)
);

-- bot 最后一条消息（每个会话一行，覆盖式更新）
CREATE TABLE IF NOT EXISTS bot_message (
    scope_type  TEXT    NOT NULL,
    scope_id    TEXT    NOT NULL,
    platform_id TEXT    NOT NULL DEFAULT '',
    ts          INTEGER NOT NULL,
    kind        TEXT    NOT NULL DEFAULT 'normal',
    command     TEXT    NOT NULL DEFAULT '',
    preview     TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (scope_type, scope_id)
);
CREATE INDEX IF NOT EXISTS idx_bot_msg_ts ON bot_message(ts DESC);

-- 用量 / 事件日志
CREATE TABLE IF NOT EXISTS usage_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    platform_id   TEXT    NOT NULL DEFAULT '',
    umo           TEXT    NOT NULL DEFAULT '',
    scope_type    TEXT    NOT NULL DEFAULT '',
    scope_id      TEXT    NOT NULL DEFAULT '',
    sender_id     TEXT    NOT NULL DEFAULT '',
    group_id      TEXT    NOT NULL DEFAULT '',
    kind          TEXT    NOT NULL DEFAULT 'llm',
    command_name  TEXT    NOT NULL DEFAULT '',
    provider_id   TEXT    NOT NULL DEFAULT '',
    model         TEXT    NOT NULL DEFAULT '',
    tok_in_other  INTEGER NOT NULL DEFAULT 0,
    tok_in_cached INTEGER NOT NULL DEFAULT 0,
    tok_out       INTEGER NOT NULL DEFAULT 0,
    estimated     INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'ok',
    deny_reason   TEXT    NOT NULL DEFAULT '',
    latency_ms    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_ts          ON usage_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_usage_scope       ON usage_log(scope_type, scope_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_usage_kind        ON usage_log(kind, ts DESC);
CREATE INDEX IF NOT EXISTS idx_usage_sender      ON usage_log(sender_id, ts DESC);

-- 管理员操作审计
CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    actor   TEXT    NOT NULL DEFAULT '',
    action  TEXT    NOT NULL DEFAULT '',
    payload TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC);
"""

# usage_log 中可被前端筛选的字段白名单（防 SQL 注入：只允许固定列名进 SQL）
_USAGE_COLUMNS = (
    "ts", "platform_id", "umo", "scope_type", "scope_id", "sender_id", "group_id",
    "kind", "command_name", "provider_id", "model",
    "tok_in_other", "tok_in_cached", "tok_out", "estimated",
    "status", "deny_reason", "latency_ms",
)


def now_ts() -> int:
    """当前时间戳（秒）。"""
    return int(time.time())


class Store:
    """插件持久层。生命周期与插件实例一致：``open()`` / ``close()``。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def open(self) -> None:
        """建立连接、开启 WAL、建表、跑迁移并记录 schema 版本。可重复调用。"""
        if self._db is not None:
            return
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        # WAL：读写并发不互相阻塞（统计写入频繁，控制台同时查询）
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA foreign_keys=ON")
        self._db = db

        old_version = await self._schema_version(db)
        await db.executescript(SCHEMA_SQL)
        if old_version < SCHEMA_VERSION:
            await self._migrate(db, old_version)
        await db.commit()
        await self.set_setting("schema_version", str(SCHEMA_VERSION))

    @staticmethod
    async def _schema_version(db: aiosqlite.Connection) -> int:
        """读库里的 schema 版本；`settings` 表还不存在时视为 0（全新库）。"""
        try:
            async with db.execute("SELECT value FROM settings WHERE key = 'schema_version'") as cur:
                row = await cur.fetchone()
            return int(row["value"]) if row and row["value"] else 0
        except Exception:
            return 0

    @staticmethod
    async def _table_columns(db: aiosqlite.Connection, table: str) -> set[str]:
        """列出表的所有列名（表不存在时返回空集）。"""
        try:
            async with db.execute(f"PRAGMA table_info({table})") as cur:
                return {str(r["name"]) for r in await cur.fetchall()}
        except Exception:
            return set()

    @classmethod
    async def _migrate(cls, db: aiosqlite.Connection, old_version: int) -> None:
        """按版本号做结构迁移。每一步都必须可重入（靠列/表存在性判断，而不是靠版本号）。"""
        if old_version < 2:
            await cls._migrate_v1_to_v2(db)
        if old_version < 3:
            await cls._migrate_v2_to_v3(db)
        if old_version < 4:
            await cls._migrate_v3_to_v4(db)
        if old_version < 5:
            await cls._migrate_v4_to_v5(db)
        if old_version < 6:
            await cls._migrate_v5_to_v6(db)
        if old_version < 8:
            await cls._migrate_v7_to_v8(db)
        if old_version < 9:
            await cls._migrate_v8_to_v9(db)
        if old_version < 10:
            await cls._migrate_v9_to_v10(db)
        if old_version < 11:
            await cls._migrate_v10_to_v11(db)
        # v6 → v7 只新增了一张空表（group_member），上面的 SCHEMA_SQL 已经建好，
        # 没有数据要搬，所以不需要单独的迁移步骤 —— 这里留个说明避免以后误以为漏了。

    @staticmethod
    async def _migrate_v10_to_v11(db: aiosqlite.Connection) -> None:
        """v10 → v11：等级新增「是否允许用 /切换模型检测」开关（``detect_enabled``）。

        **默认 0（关）**：这条指令会真打模型、花真额度，所以新功能一律不改变既有行为 ——
        升级后所有分组都是「不允许」，要放开就在控制台对应等级上显式打开。
        按列是否存在判断，可重入。
        """
        if "detect_enabled" in await Store._table_columns(db, "quota_level"):
            return
        await db.execute(
            "ALTER TABLE quota_level ADD COLUMN detect_enabled INTEGER NOT NULL DEFAULT 0"
        )

    @staticmethod
    async def _migrate_v9_to_v10(db: aiosqlite.Connection) -> None:
        """v9 → v10：等级新增「是否允许切换模型」开关（``switch_enabled``）。

        **默认 0（关）**：v1.3.3 的语义是「名单非空 = 开放切换」，v1.3.4 起改成
        「开关打开才允许切，名单为空时用兜底三项（当前 / 系统默认 / 备用）」。
        旧数据升级后一律是「只读」——用户还能查看自己用的是什么模型，但切不了，
        符合「默认不放开自助切换」的取舍（要放开就在控制台把开关打开）。
        """
        if "switch_enabled" in await Store._table_columns(db, "quota_level"):
            return
        await db.execute(
            "ALTER TABLE quota_level ADD COLUMN switch_enabled INTEGER NOT NULL DEFAULT 0"
        )

    @staticmethod
    async def _migrate_v8_to_v9(db: aiosqlite.Connection) -> None:
        """v8 → v9：等级新增「可切换模型」列（``switch_providers``）。

        只在 ``quota_level`` 上加一列，默认空串 = 该等级不开放自助切换（新功能默认不改变
        任何既有行为）。按列是否存在判断，可重入。
        """
        if "switch_providers" in await Store._table_columns(db, "quota_level"):
            return
        await db.execute(
            "ALTER TABLE quota_level ADD COLUMN switch_providers TEXT NOT NULL DEFAULT ''"
        )

    @staticmethod
    async def _migrate_v7_to_v8(db: aiosqlite.Connection) -> None:
        """v7 → v8：好友等级分场景（私聊 / 群聊）。

        ``subject_level.level_id`` 从 NOT NULL 放宽为可空，并新增
        ``level_id_group``（群聊专属等级，NULL = 跟随私聊）。
        SQLite 改不了列约束，走「建新表搬数据再改名」。
        """
        cols = await Store._table_columns(db, "subject_level")
        if "level_id_group" in cols:
            return
        await db.execute(
            """CREATE TABLE subject_level_new (
                   scope_type     TEXT    NOT NULL,
                   scope_id       TEXT    NOT NULL,
                   level_id       INTEGER,
                   level_id_group INTEGER,
                   updated_at     INTEGER NOT NULL,
                   PRIMARY KEY (scope_type, scope_id)
               )"""
        )
        await db.execute(
            "INSERT INTO subject_level_new(scope_type, scope_id, level_id, level_id_group, updated_at) "
            "SELECT scope_type, scope_id, level_id, NULL, updated_at FROM subject_level"
        )
        await db.execute("DROP TABLE subject_level")
        await db.execute("ALTER TABLE subject_level_new RENAME TO subject_level")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_subject_level ON subject_level(level_id)")

    @staticmethod
    async def _migrate_v5_to_v6(db: aiosqlite.Connection) -> None:
        """v5 → v6：引入「场景」维度（私聊 / 群聊分开管）。

        两件事：

        1. ``policy`` 加 ``scene`` 列。唯一键要从 ``(scope_type, scope_id, feature)`` 变成
           加上 ``scene``，而 SQLite 改不了唯一约束，所以重建表。旧行一律填 ``''``（通用）
           —— 判定时通用规则在两个场景都生效，所以**升级不会改变任何既有行为**。
        2. ``quota_level`` 加「群聊场景」的两列默认权限（只有 kind='user' 的等级会用）。
           默认 ``inherit`` = 跟随主值，同样是「行为不变」。
        """
        if "scene" not in await Store._table_columns(db, "policy"):
            await db.executescript(
                """
                DROP INDEX IF EXISTS idx_policy_scope;
                ALTER TABLE policy RENAME TO policy_v5;
                CREATE TABLE policy (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_type  TEXT    NOT NULL,
                    scope_id    TEXT    NOT NULL,
                    feature     TEXT    NOT NULL DEFAULT 'llm',
                    scene       TEXT    NOT NULL DEFAULT '',
                    effect      TEXT    NOT NULL,
                    note        TEXT    NOT NULL DEFAULT '',
                    updated_at  INTEGER NOT NULL,
                    UNIQUE (scope_type, scope_id, feature, scene)
                );
                INSERT INTO policy(id, scope_type, scope_id, feature, scene, effect, note, updated_at)
                    SELECT id, scope_type, scope_id, feature, '', effect, note, updated_at
                    FROM policy_v5;
                DROP TABLE policy_v5;
                CREATE INDEX IF NOT EXISTS idx_policy_scope ON policy(scope_type, scope_id);
                """,
            )
        cols = await Store._table_columns(db, "quota_level")
        for name in ("effect_group", "command_effect_group"):
            if name in cols:
                continue
            await db.execute(
                f"ALTER TABLE quota_level ADD COLUMN {name} TEXT NOT NULL DEFAULT 'inherit'"
            )

    @staticmethod
    async def _migrate_v4_to_v5(db: aiosqlite.Connection) -> None:
        """v4 → v5：等级新增「默认指令权限」列（``command_effect``）。"""
        if "command_effect" in await Store._table_columns(db, "quota_level"):
            return
        await db.execute(
            "ALTER TABLE quota_level ADD COLUMN command_effect TEXT NOT NULL DEFAULT 'inherit'"
        )

    @staticmethod
    async def _migrate_v3_to_v4(db: aiosqlite.Connection) -> None:
        """v3 → v4：删掉等级里多余的 ``model`` 列。

        模型选择以「提供商」为单位（AstrBot 里一个提供商绑定一个模型），
        再单独存一个模型名会让配置含混，故移除。重建表以兼容旧版 SQLite。
        """
        if "model" not in await Store._table_columns(db, "quota_level"):
            return
        await db.executescript(
            """
            ALTER TABLE quota_level RENAME TO quota_level_v3;
            CREATE TABLE quota_level (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                kind                 TEXT    NOT NULL,
                name                 TEXT    NOT NULL,
                description          TEXT    NOT NULL DEFAULT '',
                effect               TEXT    NOT NULL DEFAULT 'inherit',
                sort_order           INTEGER NOT NULL DEFAULT 0,
                provider_id          TEXT    NOT NULL DEFAULT '',
                fallback_provider_id TEXT    NOT NULL DEFAULT '',
                updated_at           INTEGER NOT NULL,
                UNIQUE (kind, name)
            );
            INSERT INTO quota_level(id, kind, name, description, effect, sort_order,
                                    provider_id, fallback_provider_id, updated_at)
                SELECT id, kind, name, description, effect, sort_order,
                       provider_id, fallback_provider_id, updated_at
                FROM quota_level_v3;
            DROP TABLE quota_level_v3;
            """,
        )

    @staticmethod
    async def _migrate_v2_to_v3(db: aiosqlite.Connection) -> None:
        """v2 → v3：等级新增「模型路由」三列（主提供商 / 模型名 / 备用提供商）。

        SQLite 的 ``ALTER TABLE ADD COLUMN`` 对「常量默认值」的列是可用的，
        且这里按**列是否存在**判断，重复执行安全。
        """
        cols = await Store._table_columns(db, "quota_level")
        if not cols:
            return  # 全新库：SCHEMA_SQL 已经带上这三列
        for name in ("provider_id", "model", "fallback_provider_id"):
            if name in cols:
                continue
            await db.execute(
                f"ALTER TABLE quota_level ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
            )

    @staticmethod
    async def _migrate_v1_to_v2(db: aiosqlite.Connection) -> None:
        """v1 → v2：额度与用量分离。

        v1 的 ``llm_quota.used_tokens`` 同时承担「限额」和「已用量」，引入等级模板后
        必须拆开（换额度模板不该清空用量），故把已用量搬进 ``usage_counter`` 并重建
        ``llm_quota`` 去掉该列。
        """
        if "used_tokens" not in await Store._table_columns(db, "llm_quota"):
            return
        # 1) 已用量迁入 usage_counter（v1 只有 user/group 两种 scope）
        await db.execute(
            """
            INSERT INTO usage_counter(scope_type, scope_id, period, used_tokens, reset_at, updated_at)
            SELECT scope_type, scope_id, period, used_tokens, reset_at, updated_at
            FROM llm_quota WHERE used_tokens > 0
            ON CONFLICT(scope_type, scope_id, period) DO UPDATE SET
                used_tokens = excluded.used_tokens,
                reset_at = excluded.reset_at
            """,
        )
        # 2) 重建 llm_quota（去掉 used_tokens 列）。
        #    注意先 DROP INDEX：SQLite 重命名表时索引会跟着走，不先删掉的话
        #    新表建索引会因为「同名索引已存在」而被 IF NOT EXISTS 跳过，导致新表没有索引。
        await db.executescript(
            """
            DROP INDEX IF EXISTS idx_quota_scope;
            ALTER TABLE llm_quota RENAME TO llm_quota_v1;
            CREATE TABLE llm_quota (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_type   TEXT    NOT NULL,
                scope_id     TEXT    NOT NULL,
                period       TEXT    NOT NULL,
                limit_tokens INTEGER NOT NULL,
                mode         TEXT    NOT NULL DEFAULT 'enforce',
                reset_at     INTEGER,
                updated_at   INTEGER NOT NULL,
                UNIQUE (scope_type, scope_id, period)
            );
            INSERT INTO llm_quota(id, scope_type, scope_id, period, limit_tokens, mode, reset_at, updated_at)
                SELECT id, scope_type, scope_id, period, limit_tokens, mode, reset_at, updated_at
                FROM llm_quota_v1;
            DROP TABLE llm_quota_v1;
            CREATE INDEX IF NOT EXISTS idx_quota_scope ON llm_quota(scope_type, scope_id);
            """,
        )

    async def close(self) -> None:
        """关闭连接（幂等）。"""
        db, self._db = self._db, None
        if db is not None:
            try:
                await db.close()
            except Exception:
                pass

    @property
    def ready(self) -> bool:
        return self._db is not None

    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store 尚未 open()")
        return self._db

    # ------------------------------------------------------------------ #
    # settings（内部 KV）
    # ------------------------------------------------------------------ #
    async def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        async with self._conn().execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, str(value), now_ts()),
        )
        await db.commit()

    # ------------------------------------------------------------------ #
    # 权限规则
    # ------------------------------------------------------------------ #
    async def get_effect(
        self,
        scope_type: str,
        scope_id: str,
        feature: str = "llm",
        scene: str = SCENE_ANY,
    ) -> Optional[str]:
        """取某对象在某场景下的权限效果；无记录返回 None（表示"继承上级"）。

        ``scene=''`` 只查通用规则（不会顺带命中场景专属的），要「场景回落通用」请用
        :meth:`resolve_policy`。
        """
        async with self._conn().execute(
            "SELECT effect FROM policy WHERE scope_type = ? AND scope_id = ? "
            "AND feature = ? AND scene = ?",
            (scope_type, str(scope_id), feature, str(scene or "")),
        ) as cur:
            row = await cur.fetchone()
        return row["effect"] if row else None

    async def resolve_policy(
        self,
        scope_type: str,
        scope_id: str,
        feature: str = "llm",
        scene: str = SCENE_ANY,
    ) -> Optional[str]:
        """按「场景专属 → 通用」取生效效果；都没有返回 None。

        这是判定链之外（控制台展示、单点查询）用的语义，与 ``gate`` 里的一致。
        """
        scene = str(scene or "")
        if scene:
            got = await self.get_effect(scope_type, scope_id, feature, scene)
            if got:
                return got
        return await self.get_effect(scope_type, scope_id, feature, SCENE_ANY)

    async def set_policy(
        self,
        scope_type: str,
        scope_id: str,
        effect: str,
        feature: str = "llm",
        note: str = "",
        scene: str = SCENE_ANY,
    ) -> None:
        """写入/更新一条权限规则。``effect='inherit'`` 与删除等价。

        ``scene`` 只对 ``scope_type='user'`` 有意义（群 / 全局规则本来就不分场景）；
        删「恢复继承」时也是按``(对象, feature, scene)``精确删，不会误伤另一个场景的规则。
        """
        db = self._conn()
        scene = str(scene or "")
        if effect == "inherit":
            await db.execute(
                "DELETE FROM policy WHERE scope_type = ? AND scope_id = ? AND feature = ? AND scene = ?",
                (scope_type, str(scope_id), feature, scene),
            )
        else:
            await db.execute(
                "INSERT INTO policy(scope_type, scope_id, feature, scene, effect, note, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(scope_type, scope_id, feature, scene) DO UPDATE SET "
                "effect = excluded.effect, note = excluded.note, updated_at = excluded.updated_at",
                (scope_type, str(scope_id), feature, scene, effect, note, now_ts()),
            )
        await db.commit()

    async def list_policies(
        self,
        scope_type: Optional[str] = None,
        feature: Optional[str] = None,
        scene: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """列出规则；``scope_type`` / ``feature`` / ``scene`` 都可选过滤。

        ``scene=None`` = 不过滤（含通用与场景专属两种）；``scene=''`` = 只要通用规则。
        """
        sql = (
            "SELECT scope_type, scope_id, feature, scene, effect, note, updated_at "
            "FROM policy WHERE 1 = 1"
        )
        args: list[Any] = []
        if scene is not None:
            sql += " AND scene = ?"
            args.append(str(scene))
        if feature:
            sql += " AND feature = ?"
            args.append(feature)
        if scope_type:
            sql += " AND scope_type = ?"
            args.append(scope_type)
        async with self._conn().execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def effect_map(
        self,
        scope_type: str,
        feature: str = "llm",
    ) -> dict[str, dict[str, str]]:
        """取某作用域下的 ``{scope_id: {scene: effect}}``，供闸门热路径全内存判定。

        保留 scene 这一层，是为了让判定链能按「场景专属 → 通用」回落，
        而不用在热路径上查两次库。``scene=''`` 就是通用（旧数据）。
        """
        rows = await self.list_policies(scope_type=scope_type, feature=feature)
        out: dict[str, dict[str, str]] = {}
        for r in rows:
            out.setdefault(str(r["scope_id"]), {})[str(r.get("scene") or "")] = str(r["effect"])
        return out

    async def migrate_model_choice(self) -> int:
        """v1.3.12 一次性迁移：「用户自己切换的模型」并入「专属模型」。

        背景：旧版把 ``/切换模型`` 的结果按**用户**存（``model_choice``，群/私聊各一份），
        于是「在 A 群切换 → 这个人在**所有**群里都换了模型」—— 用户反馈的跨群串号。
        新语义：切换 = 改**专属模型**（私聊 = 用户的、群聊 = 群的），见 ``model_switch``。

        - 私聊场景的旧选择 → 迁成该用户的专属模型（**他还没有专属模型时才迁**，
          不覆盖管理员显式钉的值）；
        - 群聊场景的旧选择 → 直接删：旧语义是「按人、跨所有群生效」，本来就不对，
          没有可迁移的对象语义（用户在群里重发一次 /切换模型 即可）。
        - 幂等：跑完 ``model_choice`` 表为空，再跑一遍是 no-op。

        Returns:
            实际迁成专属模型的条数（纯删除不计入）。
        """
        db = self._conn()
        rows = await self.list_policies(feature=MODEL_CHOICE_FEATURE)
        moved = 0
        for r in rows:
            scope_type = str(r["scope_type"])
            scope_id = str(r["scope_id"])
            scene = str(r.get("scene") or "")
            effect = str(r["effect"])
            if (
                scope_type == "user"
                and scene in ("", SCENE_ANY, "private")
                and effect not in ("", "inherit")
            ):
                got = await self.get_effect("user", scope_id, MODEL_FEATURE, SCENE_ANY)
                if not got:
                    await self.set_policy(
                        "user",
                        scope_id,
                        effect,
                        feature=MODEL_FEATURE,
                        note="由旧版「用户切换」迁移",
                        scene=SCENE_ANY,
                    )
                    moved += 1
            await db.execute(
                "DELETE FROM policy WHERE scope_type = ? AND scope_id = ? AND feature = ? AND scene = ?",
                (scope_type, scope_id, MODEL_CHOICE_FEATURE, scene),
            )
        await db.commit()
        return moved

    async def migrate_global_quota_scene(self) -> int:
        """v1.4.0 一次性迁移：「全局默认额度」从单一 ``global:*`` 拆成私聊 / 群聊两份。

        背景：旧版全局默认额度只有一份（``scope_type='global', scope_id='*'``），
        私聊和群聊共用同一组上限；但群聊按「群」记账、私聊按「人」记账，口径不同，
        用户要求能分开配。新版闸门（``gate.layers_for``）在全局层按场景取
        ``global:private`` / ``global:group``，所以旧 ``*`` 行必须迁走，否则永远读不到。

        - ``*`` 行复制成 ``private`` 与 ``group`` 两行（目标已存在时以目标为准，不覆盖
          —— 只可能在「迁移过一次后又导入了旧备份」这种混合状态下出现）；
        - 然后删掉全部 ``*`` 行；
        - 幂等：库里没有 ``global:*`` 时是 no-op。

        Returns:
            迁移（复制）出的额度行数。
        """
        db = self._conn()
        async with db.execute(
            "SELECT id, period, limit_tokens, mode, reset_at, updated_at "
            "FROM llm_quota WHERE scope_type = 'global' AND scope_id = '*'"
        ) as cur:
            rows = await cur.fetchall()
        moved = 0
        for r in rows:
            for scene in ("private", "group"):
                got = await self.get_quota("global", scene, str(r["period"]))
                if got is None:
                    await db.execute(
                        "INSERT INTO llm_quota(scope_type, scope_id, period, limit_tokens, mode, reset_at, updated_at) "
                        "VALUES('global', ?, ?, ?, ?, ?, ?)",
                        (scene, str(r["period"]), int(r["limit_tokens"]), str(r["mode"]), r["reset_at"], int(r["updated_at"])),
                    )
                    moved += 1
        await db.execute("DELETE FROM llm_quota WHERE scope_type = 'global' AND scope_id = '*'")
        await db.commit()
        return moved

    async def command_policies(
        self,
    ) -> dict[str, dict[str, dict[str, dict[str, str]]]]:
        """所有指令规则，形如 ``{指令名: {scope_type: {scope_id: {scene: effect}}}}``。

        （闸门热路径用；scene 层同 :meth:`effect_map`。）
        """
        async with self._conn().execute(
            "SELECT feature, scope_type, scope_id, scene, effect FROM policy WHERE feature LIKE ?",
            (f"{COMMAND_FEATURE_PREFIX}%",),
        ) as cur:
            out: dict[str, dict[str, dict[str, dict[str, str]]]] = {}
            for r in await cur.fetchall():
                cmd = command_of_feature(str(r["feature"]))
                if cmd:
                    # 注意：这里是 aiosqlite.Row，**没有 dict.get()**，只能下标取值
                    out.setdefault(cmd, {}).setdefault(str(r["scope_type"]), {}).setdefault(
                        str(r["scope_id"]), {}
                    )[str(r["scene"] or "")] = str(r["effect"])
            return out

    async def delete_stale_command_policies(self, alive: list[str]) -> int:
        """清理已失效的指令规则（指令被卸载/改名后残留的行）。返回删除行数。

        Args:
            alive: 当前注册表里仍然存在的指令名列表。
        """
        features = [command_feature(c) for c in alive if str(c or "").strip()]
        db = self._conn()
        if features:
            marks = ", ".join("?" for _ in features)
            cur = await db.execute(
                f"DELETE FROM policy WHERE feature LIKE ? AND feature NOT IN ({marks})",
                [f"{COMMAND_FEATURE_PREFIX}%", *features],
            )
        else:
            cur = await db.execute(
                "DELETE FROM policy WHERE feature LIKE ?", (f"{COMMAND_FEATURE_PREFIX}%",)
            )
        await db.commit()
        return cur.rowcount or 0

    # ------------------------------------------------------------------ #
    # 模型（提供商）引用的一致性：AstrBot 删除 / 禁用模型后的悬空引用
    # ------------------------------------------------------------------ #
    async def model_ref_rows(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """取出所有引用了模型 id 的持久化行：``(等级全表, 专属模型 policy 行)``。

        只给 ``provider_refs.collect_refs`` 当输入；**不碰** ``usage_log`` /
        ``audit_log`` —— 历史用量与审计里的旧 id 是账目，模型删了也要留着对账。
        """
        async with self._conn().execute("SELECT * FROM quota_level") as cur:
            levels = [dict(r) for r in await cur.fetchall()]
        marks = ", ".join("?" for _ in PROVIDER_FEATURES)
        async with self._conn().execute(
            "SELECT scope_type, scope_id, feature, scene, effect FROM policy "
            f"WHERE feature IN ({marks})",
            list(PROVIDER_FEATURES),
        ) as cur:
            policies = [dict(r) for r in await cur.fetchall()]
        return levels, policies

    async def prune_provider_refs(self, ids: Any) -> dict[str, int]:
        """**用户主动触发**的定向清理：把指定模型 id 从持久化配置里摘掉。

        只清用户勾选的 ``ids``，绝不「顺手清所有当前不可用的」——禁用是可逆的、
        加载是有时序的，全量自动清理会抹掉用户配好的等级路由。

        - 等级主 / 备模型：命中的字段清空（等于回落 AstrBot 默认模型）；
        - 等级可切换名单：从 JSON 里剔除命中的 id，其余保持顺序；
        - ``policy`` 里 ``feature in (model, model_choice)`` 且 effect 命中：整行删除
          （等于该对象恢复「跟随等级 / 系统默认」）；
        - 用量、审计、成员缓存一律不动。

        Returns: ``{levels, level_fields, switch_removed, policies}`` 各项清理计数。
        """
        wanted = {str(p or "").strip() for p in (ids or ()) if str(p or "").strip()}
        stats = {"levels": 0, "level_fields": 0, "switch_removed": 0, "policies": 0}
        if not wanted:
            return stats
        db = self._conn()

        levels, _ = await self.model_ref_rows()
        for lv in levels:
            changed: dict[str, Any] = {}
            for field in ("provider_id", "fallback_provider_id"):
                if str(lv.get(field) or "").strip() in wanted:
                    changed[field] = ""
                    stats["level_fields"] += 1
            old_list = parse_provider_list(lv.get("switch_providers"))
            new_list = [p for p in old_list if p not in wanted]
            if len(new_list) != len(old_list):
                changed["switch_providers"] = dump_provider_list(new_list)
                stats["switch_removed"] += len(old_list) - len(new_list)
            if not changed:
                continue
            sets = ", ".join(f"{k} = ?" for k in changed)
            await db.execute(
                f"UPDATE quota_level SET {sets}, updated_at = ? WHERE id = ?",
                [*changed.values(), now_ts(), int(lv["id"])],
            )
            stats["levels"] += 1

        marks = ", ".join("?" for _ in wanted)
        feat_marks = ", ".join("?" for _ in PROVIDER_FEATURES)
        cur = await db.execute(
            f"DELETE FROM policy WHERE feature IN ({feat_marks}) AND effect IN ({marks})",
            [*PROVIDER_FEATURES, *sorted(wanted)],
        )
        stats["policies"] = cur.rowcount or 0
        await db.commit()
        return stats

    # ------------------------------------------------------------------ #
    # 额度
    # ------------------------------------------------------------------ #
    async def get_quota(self, scope_type: str, scope_id: str, period: str) -> Optional[dict[str, Any]]:
        async with self._conn().execute(
            "SELECT * FROM llm_quota WHERE scope_type = ? AND scope_id = ? AND period = ?",
            (scope_type, str(scope_id), period),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_quotas(self, scope_type: Optional[str] = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM llm_quota"
        args: list[Any] = []
        if scope_type:
            sql += " WHERE scope_type = ?"
            args.append(scope_type)
        sql += " ORDER BY updated_at DESC"
        async with self._conn().execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def upsert_quota(
        self,
        scope_type: str,
        scope_id: str,
        period: str,
        limit_tokens: int,
        mode: str = "enforce",
        reset_at: Optional[int] = None,
    ) -> None:
        """新建或更新一条限额规则。

        v2 起这里**只写上限**，已用量在 ``usage_counter``；因此改额度不会清零已用量
        （要清零请用 :meth:`reset_used`）。``scope_type`` 支持
        ``user`` / ``group``（对象专属）与 ``level`` / ``global``（模板）。
        """
        db = self._conn()
        await db.execute(
            "INSERT INTO llm_quota(scope_type, scope_id, period, limit_tokens, mode, reset_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope_type, scope_id, period) DO UPDATE SET "
            "limit_tokens = excluded.limit_tokens, mode = excluded.mode, "
            "reset_at = excluded.reset_at, updated_at = excluded.updated_at",
            (scope_type, str(scope_id), period, int(limit_tokens), mode, reset_at, now_ts()),
        )
        await db.commit()

    async def delete_quota(self, scope_type: str, scope_id: str, period: Optional[str] = None) -> None:
        db = self._conn()
        if period:
            await db.execute(
                "DELETE FROM llm_quota WHERE scope_type = ? AND scope_id = ? AND period = ?",
                (scope_type, str(scope_id), period),
            )
        else:
            await db.execute(
                "DELETE FROM llm_quota WHERE scope_type = ? AND scope_id = ?",
                (scope_type, str(scope_id)),
            )
        await db.commit()

    async def add_used(
        self,
        scope_type: str,
        scope_id: str,
        tokens: int,
        reset_at_map: Optional[dict[str, Optional[int]]] = None,
    ) -> int:
        """把一次消费累加进 ``usage_counter``（日 / 月 / 累计三个周期各一条）。

        与 v1 的关键区别：**即使该对象没有任何额度配置也会累加**。因为等级额度与全局额度
        都是「模板」，判断是否超限时必须用**该对象自己的**用量，所以用量必须无条件记账。

        Args:
            reset_at_map: ``{period: 重置时间戳}``，仅在**首次创建**该周期计数行时写入；
                已存在的行不动 ``reset_at``（否则每次调用都会把重置时间往后推，永远不重置）。

        Returns:
            实际累加的 token 数（``tokens<=0`` 时为 0）。
        """
        if tokens <= 0:
            return 0
        reset_at_map = reset_at_map or {}
        now = now_ts()
        rows = [
            (scope_type, str(scope_id), period, int(tokens), reset_at_map.get(period), now)
            for period in PERIODS
        ]
        db = self._conn()
        await db.executemany(
            "INSERT INTO usage_counter(scope_type, scope_id, period, used_tokens, reset_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope_type, scope_id, period) DO UPDATE SET "
            "used_tokens = used_tokens + excluded.used_tokens, updated_at = excluded.updated_at",
            rows,
        )
        await db.commit()
        return int(tokens)

    async def reset_used(
        self,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        period: Optional[str] = None,
        reset_at: Optional[int] = None,
    ) -> int:
        """清零已用量（可按对象/周期过滤），返回受影响行数。作用于 ``usage_counter``。"""
        sql = "UPDATE usage_counter SET used_tokens = 0, updated_at = ?"
        args: list[Any] = [now_ts()]
        if reset_at is not None:
            sql += ", reset_at = ?"
            args.append(reset_at)
        where, wargs = self._scope_where(scope_type, scope_id, period)
        if where:
            sql += " WHERE " + where
            args.extend(wargs)
        db = self._conn()
        cur = await db.execute(sql, args)
        await db.commit()
        return cur.rowcount or 0

    @staticmethod
    def _scope_where(
        scope_type: Optional[str],
        scope_id: Optional[str],
        period: Optional[str],
    ) -> tuple[str, list[Any]]:
        parts: list[str] = []
        args: list[Any] = []
        if scope_type:
            parts.append("scope_type = ?")
            args.append(scope_type)
        if scope_id:
            parts.append("scope_id = ?")
            args.append(str(scope_id))
        if period:
            parts.append("period = ?")
            args.append(period)
        return " AND ".join(parts), args

    # ------------------------------------------------------------------ #
    # 等级（kind=user 的私聊等级 / kind=group 的群聊等级）
    # ------------------------------------------------------------------ #
    async def list_levels(self, kind: Optional[str] = None) -> list[dict[str, Any]]:
        """列出等级；``kind`` 为空时返回私聊+群聊全部（按 kind、排序号）。"""
        sql = "SELECT * FROM quota_level"
        args: list[Any] = []
        if kind:
            sql += " WHERE kind = ?"
            args.append(kind)
        sql += " ORDER BY kind, sort_order, id"
        async with self._conn().execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_level(self, level_id: int) -> Optional[dict[str, Any]]:
        async with self._conn().execute("SELECT * FROM quota_level WHERE id = ?", (int(level_id),)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def upsert_level(
        self,
        kind: str,
        name: str,
        description: str = "",
        effect: str = "inherit",
        sort_order: int = 0,
        level_id: Optional[int] = None,
        provider_id: str = "",
        fallback_provider_id: str = "",
        command_effect: str = "inherit",
        effect_group: str = "inherit",
        command_effect_group: str = "inherit",
        switch_providers: Any = "",
        switch_enabled: Any = False,
        detect_enabled: Any = False,
    ) -> int:
        """新建 / 更新等级，返回等级 id（``level_id`` 为空则按 ``(kind, name)`` 新建）。

        - ``effect`` / ``command_effect``：等级的默认 **LLM / 指令** 权限（主场景 = 私聊）；
        - ``effect_group`` / ``command_effect_group``：**群聊场景**下的那一套，
          只对 ``kind='user'`` 有意义；``inherit`` = 跟随主值（旧数据的语义）；
        - ``provider_id`` / ``fallback_provider_id`` 是模型路由：属于该等级的对象
          走指定提供商，主提供商不可用（未加载 / 熔断）时用备用那个；
        - ``switch_providers``：该等级**允许用户自助切换**的提供商 id 列表
          （v9，收 list 或 JSON 文本；空 = 用兜底三项：当前 / 系统默认 / 备用）；
        - ``switch_enabled``：该等级**是否允许切换**（v10，默认假 = 只读）；
        - ``detect_enabled``：该等级**是否允许用 ``/切换模型检测``**（v11，默认假）。
          它与 ``switch_enabled`` 是两件事：检测要真花钱打模型，只看不切的分组未必愿意掏这笔钱。
        """
        db = self._conn()
        switch_json = dump_provider_list(switch_providers)
        switch_flag = _as_flag(switch_enabled)
        detect_flag = _as_flag(detect_enabled)
        if level_id:
            await db.execute(
                "UPDATE quota_level SET kind = ?, name = ?, description = ?, effect = ?, "
                "command_effect = ?, effect_group = ?, command_effect_group = ?, "
                "sort_order = ?, provider_id = ?, fallback_provider_id = ?, "
                "switch_providers = ?, switch_enabled = ?, detect_enabled = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    kind,
                    name,
                    description,
                    effect,
                    command_effect,
                    effect_group,
                    command_effect_group,
                    int(sort_order),
                    str(provider_id or ""),
                    str(fallback_provider_id or ""),
                    switch_json,
                    switch_flag,
                    detect_flag,
                    now_ts(),
                    int(level_id),
                ),
            )
            await db.commit()
            return int(level_id)
        await db.execute(
            "INSERT INTO quota_level(kind, name, description, effect, command_effect, "
            "effect_group, command_effect_group, sort_order, "
            "provider_id, fallback_provider_id, switch_providers, switch_enabled, "
            "detect_enabled, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(kind, name) DO UPDATE SET "
            "description = excluded.description, effect = excluded.effect, "
            "command_effect = excluded.command_effect, "
            "effect_group = excluded.effect_group, "
            "command_effect_group = excluded.command_effect_group, "
            "sort_order = excluded.sort_order, provider_id = excluded.provider_id, "
            "fallback_provider_id = excluded.fallback_provider_id, "
            "switch_providers = excluded.switch_providers, "
            "switch_enabled = excluded.switch_enabled, "
            "detect_enabled = excluded.detect_enabled, "
            "updated_at = excluded.updated_at",
            (
                kind,
                name,
                description,
                effect,
                command_effect,
                effect_group,
                command_effect_group,
                int(sort_order),
                str(provider_id or ""),
                str(fallback_provider_id or ""),
                switch_json,
                switch_flag,
                detect_flag,
                now_ts(),
            ),
        )
        await db.commit()
        async with db.execute(
            "SELECT id FROM quota_level WHERE kind = ? AND name = ?", (kind, name)
        ) as cur:
            row = await cur.fetchone()
        return int(row["id"]) if row else 0

    async def delete_level(self, level_id: int) -> None:
        """删除等级：连带删掉该等级的额度模板与所有归级记录（对象回到「无等级」）。"""
        db = self._conn()
        await db.execute("DELETE FROM quota_level WHERE id = ?", (int(level_id),))
        await db.execute(
            "DELETE FROM llm_quota WHERE scope_type = 'level' AND scope_id = ?", (str(level_id),)
        )
        await db.execute("DELETE FROM subject_level WHERE level_id = ?", (int(level_id),))
        await db.commit()

    async def level_counts(self) -> dict[int, int]:
        """每个等级下的对象数量 ``{level_id: count}``（控制台列表展示用）。"""
        async with self._conn().execute(
            "SELECT level_id, COUNT(*) AS c FROM subject_level GROUP BY level_id"
        ) as cur:
            return {int(r["level_id"]): int(r["c"]) for r in await cur.fetchall()}

    # ------------------------------------------------------------------ #
    # 对象归级
    # ------------------------------------------------------------------ #
    async def subject_level_map(self, scope_type: str) -> dict[str, int]:
        """取某作用域下 ``{scope_id: level_id}`` 映射（闸门热路径全内存判定用）。

        v8 起好友的 ``level_id`` 可为 NULL（私聊未归级但群聊归了级），NULL 不进映射。
        """
        async with self._conn().execute(
            "SELECT scope_id, level_id FROM subject_level "
            "WHERE scope_type = ? AND level_id IS NOT NULL",
            (scope_type,),
        ) as cur:
            return {str(r["scope_id"]): int(r["level_id"]) for r in await cur.fetchall()}

    async def subject_level_group_map(self) -> dict[str, int]:
        """好友的**群聊专属**等级 ``{uin: level_id}``（NULL / 空不进映射）。

        群聊场景里好友用这个等级；没配 = 跟随私聊等级。
        """
        async with self._conn().execute(
            "SELECT scope_id, level_id_group FROM subject_level "
            "WHERE scope_type = 'user' AND level_id_group IS NOT NULL"
        ) as cur:
            return {str(r["scope_id"]): int(r["level_id_group"]) for r in await cur.fetchall()}

    async def get_subject_level(self, scope_type: str, scope_id: str) -> Optional[int]:
        async with self._conn().execute(
            "SELECT level_id FROM subject_level WHERE scope_type = ? AND scope_id = ?",
            (scope_type, str(scope_id)),
        ) as cur:
            row = await cur.fetchone()
        return int(row["level_id"]) if row else None

    async def set_subject_level(self, scope_type: str, scope_id: str, level_id: Optional[int]) -> None:
        """给对象设定等级；``level_id`` 为空表示取消等级（删除记录）。

        群对象 / 导入路径用这个（不分场景）；好友分场景请用 :meth:`set_user_level_scene`。
        """
        db = self._conn()
        if not level_id:
            await db.execute(
                "DELETE FROM subject_level WHERE scope_type = ? AND scope_id = ?",
                (scope_type, str(scope_id)),
            )
        else:
            await db.execute(
                "INSERT INTO subject_level(scope_type, scope_id, level_id, updated_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(scope_type, scope_id) DO UPDATE SET "
                "level_id = excluded.level_id, updated_at = excluded.updated_at",
                (scope_type, str(scope_id), int(level_id), now_ts()),
            )
        await db.commit()

    async def get_user_level_scene(self, scope_id: str) -> tuple[Optional[int], Optional[int]]:
        """取好友的分场景等级，``(私聊等级, 群聊等级)``；没配的为 None（群聊 None = 跟随私聊）。"""
        async with self._conn().execute(
            "SELECT level_id, level_id_group FROM subject_level WHERE scope_type = 'user' AND scope_id = ?",
            (str(scope_id),),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None, None
        return (
            int(row["level_id"]) if row["level_id"] is not None else None,
            int(row["level_id_group"]) if row["level_id_group"] is not None else None,
        )

    async def set_user_level_scene(
        self,
        scope_id: str,
        *,
        private_level: Any = _LEVEL_UNSET,
        group_level: Any = _LEVEL_UNSET,
    ) -> None:
        """给好友设置**分场景**等级（v8）。

        哨兵语义（这样 API 层才能区分「不动另一场景」和「清除另一场景」）：

        - ``_LEVEL_UNSET``（默认）= 不动这个场景，保持原值；
        - ``None`` = 取消该场景等级（群聊取消 = 跟随私聊）；
        - ``int`` = 设为该等级（会话层已校验存在性与 kind）。

        两个场景最终都是「取消」时整条记录删除。
        """
        db = self._conn()
        cur_level, cur_group = await self.get_user_level_scene(scope_id)
        lv = cur_level if private_level is _LEVEL_UNSET else private_level
        lv_g = cur_group if group_level is _LEVEL_UNSET else group_level
        if lv is None and lv_g is None:
            await db.execute(
                "DELETE FROM subject_level WHERE scope_type = 'user' AND scope_id = ?",
                (str(scope_id),),
            )
        else:
            await db.execute(
                "INSERT INTO subject_level(scope_type, scope_id, level_id, level_id_group, updated_at) "
                "VALUES('user', ?, ?, ?, ?) "
                "ON CONFLICT(scope_type, scope_id) DO UPDATE SET "
                "level_id = excluded.level_id, level_id_group = excluded.level_id_group, "
                "updated_at = excluded.updated_at",
                (str(scope_id), lv, lv_g, now_ts()),
            )
        await db.commit()

    # ------------------------------------------------------------------ #
    # 用量计数（与限额规则分离）
    # ------------------------------------------------------------------ #
    async def usage_map(self, scope_type: str) -> dict[str, dict[str, dict[str, Any]]]:
        """取某作用域的用量 ``{scope_id: {period: row}}``（闸门热路径用）。"""
        async with self._conn().execute(
            "SELECT * FROM usage_counter WHERE scope_type = ?", (scope_type,)
        ) as cur:
            out: dict[str, dict[str, dict[str, Any]]] = {}
            for r in await cur.fetchall():
                row = dict(r)
                out.setdefault(str(row["scope_id"]), {})[str(row["period"])] = row
            return out

    async def get_usage(self, scope_type: str, scope_id: str) -> dict[str, dict[str, Any]]:
        """取单个对象的用量 ``{period: row}``。"""
        async with self._conn().execute(
            "SELECT * FROM usage_counter WHERE scope_type = ? AND scope_id = ?",
            (scope_type, str(scope_id)),
        ) as cur:
            return {str(r["period"]): dict(r) for r in await cur.fetchall()}

    async def counters_due(self, now: Optional[int] = None) -> list[dict[str, Any]]:
        """列出 ``reset_at`` 已到期的用量计数行（维护任务据此清零）。"""
        ts = int(now if now is not None else now_ts())
        async with self._conn().execute(
            "SELECT * FROM usage_counter WHERE reset_at IS NOT NULL AND reset_at <= ?", (ts,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------------ #
    # bot 最后一条消息
    # ------------------------------------------------------------------ #
    async def set_bot_message(
        self,
        scope_type: str,
        scope_id: str,
        kind: str,
        *,
        ts: Optional[int] = None,
        command: str = "",
        preview: str = "",
        platform_id: str = "",
    ) -> None:
        """记录 bot 在某会话里的最后一条消息（覆盖式，每个会话只留最新一条）。"""
        if not scope_id:
            return
        db = self._conn()
        await db.execute(
            "INSERT INTO bot_message(scope_type, scope_id, platform_id, ts, kind, command, preview) "
            "VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope_type, scope_id) DO UPDATE SET "
            "platform_id = excluded.platform_id, ts = excluded.ts, kind = excluded.kind, "
            "command = excluded.command, preview = excluded.preview",
            (
                scope_type,
                str(scope_id),
                platform_id,
                int(ts if ts is not None else now_ts()),
                kind,
                command[:64],
                preview[:120],
            ),
        )
        await db.commit()

    async def bot_message_summary(self, from_ts: int, to_ts: int) -> dict[str, Any]:
        """最后消息的类型分布 + 总会话数（总览页展示「bot 最近都在回什么」）。"""
        db = self._conn()
        async with db.execute(
            "SELECT kind, COUNT(*) AS cnt FROM bot_message WHERE ts >= ? AND ts <= ? GROUP BY kind",
            (int(from_ts), int(to_ts)),
        ) as cur:
            kinds = [dict(r) for r in await cur.fetchall()]
        async with db.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(ts), 0) AS latest FROM bot_message"
        ) as cur:
            row = await cur.fetchone()
        return {
            "kinds": kinds,
            "sessions": int(row["c"]) if row else 0,
            "latest_ts": int(row["latest"]) if row else 0,
        }

    async def bot_message_map(self, scope_type: str) -> dict[str, dict[str, Any]]:
        """取某作用域下所有会话的最后消息 ``{scope_id: row}``。"""
        async with self._conn().execute(
            "SELECT * FROM bot_message WHERE scope_type = ?", (scope_type,)
        ) as cur:
            return {str(r["scope_id"]): dict(r) for r in await cur.fetchall()}

    async def get_bot_message(self, scope_type: str, scope_id: str) -> Optional[dict[str, Any]]:
        async with self._conn().execute(
            "SELECT * FROM bot_message WHERE scope_type = ? AND scope_id = ?",
            (scope_type, str(scope_id)),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ #
    # 用量日志
    # ------------------------------------------------------------------ #
    async def log_usage(self, **fields: Any) -> None:
        """写一条用量/事件日志。未知字段忽略，缺失字段用表默认值。"""
        data = {k: v for k, v in fields.items() if k in _USAGE_COLUMNS}
        data.setdefault("ts", now_ts())
        cols = ", ".join(data.keys())
        marks = ", ".join("?" for _ in data)
        db = self._conn()
        await db.execute(f"INSERT INTO usage_log({cols}) VALUES({marks})", tuple(data.values()))
        await db.commit()

    async def query_usage(
        self,
        *,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        sender_id: Optional[str] = None,
        group_id: Optional[str] = None,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """分页查询明细，返回 ``{total, rows}``。

        ``group_id`` 用于「按群」过滤：群里的流水既可能是 ``scope_type='group'``
        （命中群级规则），也可能是 ``scope_type='user'``（命中对象级规则），
        而 ``group_id`` 一律记群号，所以群的维度按它过滤才不漏（与 ``subject_stats`` 口径一致）。
        """
        where, args = self._usage_where(
            from_ts, to_ts, scope_type, scope_id, sender_id, status, kind, group_id=group_id
        )
        db = self._conn()
        async with db.execute(f"SELECT COUNT(*) AS c FROM usage_log{where}", args) as cur:
            row = await cur.fetchone()
            total = int(row["c"]) if row else 0
        async with db.execute(
            f"SELECT * FROM usage_log{where} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            [*args, int(limit), int(offset)],
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        return {"total": total, "rows": rows}

    @staticmethod
    def _usage_where(
        from_ts: Optional[int],
        to_ts: Optional[int],
        scope_type: Optional[str],
        scope_id: Optional[str],
        sender_id: Optional[str],
        status: Optional[str],
        kind: Optional[str],
        group_id: Optional[str] = None,
    ) -> tuple[str, list[Any]]:
        parts: list[str] = []
        args: list[Any] = []
        if from_ts is not None:
            parts.append("ts >= ?")
            args.append(int(from_ts))
        if to_ts is not None:
            parts.append("ts <= ?")
            args.append(int(to_ts))
        for col, val in (
            ("scope_type", scope_type),
            ("scope_id", scope_id),
            ("sender_id", sender_id),
            ("group_id", group_id),
            ("status", status),
            ("kind", kind),
        ):
            if val:
                parts.append(f"{col} = ?")
                args.append(str(val))
        return (" WHERE " + " AND ".join(parts)) if parts else "", args

    async def top_commands(
        self,
        from_ts: int,
        to_ts: int,
        *,
        status: str = "ok",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """指令维度排行：``[{command, cnt}]``。

        ``status='ok'`` 取「最常触发的指令」，``status='denied'`` 取「最常被拦的指令」。
        只统计 ``kind='command'`` 的流水，与 LLM 的口径完全分开。
        """
        async with self._conn().execute(
            """SELECT command_name AS command, COUNT(*) AS cnt
               FROM usage_log
               WHERE kind = 'command' AND status = ? AND ts >= ? AND ts <= ? AND command_name <> ''
               GROUP BY command_name ORDER BY cnt DESC, command_name LIMIT ?""",
            (str(status), int(from_ts), int(to_ts), int(limit)),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def hour_heatmap(
        self,
        from_ts: int,
        to_ts: int,
        *,
        kind: str = "llm",
    ) -> list[dict[str, Any]]:
        """时段热力图：``[{dow, hour, cnt}]``。

        ``dow`` 用 SQLite 的 ``%w``（0 = 周日），``hour`` 为 0–23，均按**本地时间**分桶
        （与趋势图同口径，避免 Python 侧时区换算）。前端按 7×24 网格填充。
        """
        async with self._conn().execute(
            """SELECT CAST(strftime('%w', ts, 'unixepoch', 'localtime') AS INTEGER) AS dow,
                      CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) AS hour,
                      COUNT(*) AS cnt
               FROM usage_log
               WHERE kind = ? AND ts >= ? AND ts <= ?
               GROUP BY dow, hour""",
            (str(kind), int(from_ts), int(to_ts)),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def export_usage_csv(
        self,
        *,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        sender_id: Optional[str] = None,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 50000,
    ) -> str:
        """把明细导出成 CSV 文本（后端生成，避免前端一次性拉全量）。

        细节：
        - 开头加 **UTF-8 BOM**，否则 Excel 打开中文会乱码（Windows 上的经典坑）；
        - 时间列输出本地时间字符串，方便直接看；
        - 行数上限 ``limit``（默认 5 万）防御性兜底，避免内存与响应体爆掉。
        """
        where, args = self._usage_where(from_ts, to_ts, scope_type, scope_id, sender_id, status, kind)
        async with self._conn().execute(
            f"SELECT * FROM usage_log{where} ORDER BY ts DESC, id DESC LIMIT ?",
            [*args, int(limit)],
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        head = [
            "时间", "类型", "状态", "拒绝原因", "指令", "对象类型", "对象", "发起人",
            "群", "模型", "输入", "缓存", "输出", "合计", "估算", "延迟ms",
        ]
        buf = io.StringIO()
        buf.write("\ufeff")  # BOM：Excel 打开中文不乱码
        writer = csv.writer(buf)
        writer.writerow(head)
        for r in rows:
            total = (
                int(r.get("tok_in_other") or 0)
                + int(r.get("tok_in_cached") or 0)
                + int(r.get("tok_out") or 0)
            )
            writer.writerow(
                [
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(r.get("ts") or 0))),
                    "LLM" if str(r.get("kind")) == "llm" else "指令",
                    "被拒"
                    if str(r.get("status")) == "denied"
                    else ("失败" if str(r.get("status")) == "error" else "正常"),
                    str(r.get("deny_reason") or ""),
                    str(r.get("command_name") or ""),
                    "群" if str(r.get("scope_type")) == "group" else "好友",
                    str(r.get("scope_id") or ""),
                    str(r.get("sender_id") or ""),
                    str(r.get("group_id") or ""),
                    str(r.get("model") or ""),
                    int(r.get("tok_in_other") or 0),
                    int(r.get("tok_in_cached") or 0),
                    int(r.get("tok_out") or 0),
                    total,
                    1 if int(r.get("estimated") or 0) else 0,
                    int(r.get("latency_ms") or 0),
                ],
            )
        return buf.getvalue()

    async def summary(self, from_ts: int, to_ts: int) -> dict[str, Any]:
        """区间总览：总量、分项、趋势、榜单、拒绝原因。供控制台总览页使用。"""
        db = self._conn()
        where = "WHERE ts >= ? AND ts <= ?"
        args = [int(from_ts), int(to_ts)]

        async with db.execute(
            f"""SELECT
                    COUNT(*)                                              AS events,
                    SUM(CASE WHEN kind = 'llm' THEN 1 ELSE 0 END)         AS calls,
                    SUM(CASE WHEN status = 'denied' THEN 1 ELSE 0 END)    AS denied,
                    SUM(CASE WHEN status = 'error'  THEN 1 ELSE 0 END)    AS errors,
                    COALESCE(SUM(tok_in_other), 0)                        AS tok_in_other,
                    COALESCE(SUM(tok_in_cached), 0)                       AS tok_in_cached,
                    COALESCE(SUM(tok_out), 0)                             AS tok_out,
                    SUM(estimated)                                        AS estimated,
                    COUNT(DISTINCT CASE WHEN sender_id <> '' THEN sender_id END) AS users,
                    COUNT(DISTINCT CASE WHEN group_id  <> '' THEN group_id  END) AS groups,
                    COALESCE(AVG(CASE WHEN latency_ms > 0 THEN latency_ms END), 0) AS avg_latency
                FROM usage_log {where}""",
            args,
        ) as cur:
            totals = dict(await cur.fetchone() or {})

        # 按本地时间分桶的趋势：跨度 ≤ 48h 用**小时桶**（今日视图的 x 轴是「小时:00」，
        # 不然一整天只有一个点），更长跨度按天
        hourly = (int(to_ts) - int(from_ts)) <= 48 * 3600
        bucket = "%H:00" if hourly else "%Y-%m-%d"
        async with db.execute(
            f"""SELECT strftime('{bucket}', ts, 'unixepoch', 'localtime') AS day,
                       COALESCE(SUM(tok_in_other + tok_in_cached + tok_out), 0) AS tokens,
                       SUM(CASE WHEN kind = 'llm' THEN 1 ELSE 0 END) AS calls,
                       SUM(CASE WHEN status = 'denied' THEN 1 ELSE 0 END) AS denied
                FROM usage_log {where}
                GROUP BY day ORDER BY day""",
            args,
        ) as cur:
            trend = [dict(r) for r in await cur.fetchall()]

        async with db.execute(
            f"""SELECT scope_type, scope_id,
                       COALESCE(SUM(tok_in_other + tok_in_cached + tok_out), 0) AS tokens,
                       COUNT(*) AS events
                FROM usage_log {where} AND scope_id <> ''
                GROUP BY scope_type, scope_id ORDER BY tokens DESC LIMIT 20""",
            args,
        ) as cur:
            top_scopes = [dict(r) for r in await cur.fetchall()]

        async with db.execute(
            f"""SELECT COALESCE(NULLIF(model, ''), '(未知)') AS model,
                       COALESCE(SUM(tok_in_other + tok_in_cached + tok_out), 0) AS tokens
                FROM usage_log {where} AND kind = 'llm'
                GROUP BY model ORDER BY tokens DESC LIMIT 12""",
            args,
        ) as cur:
            by_model = [dict(r) for r in await cur.fetchall()]

        async with db.execute(
            f"""SELECT COALESCE(NULLIF(deny_reason, ''), '(其它)') AS reason, COUNT(*) AS cnt
                FROM usage_log {where} AND status = 'denied'
                GROUP BY reason ORDER BY cnt DESC""",
            args,
        ) as cur:
            deny_reasons = [dict(r) for r in await cur.fetchall()]

        totals["tok_total"] = (
            int(totals.get("tok_in_other") or 0)
            + int(totals.get("tok_in_cached") or 0)
            + int(totals.get("tok_out") or 0)
        )
        return {
            "range": {"from": int(from_ts), "to": int(to_ts)},
            "totals": totals,
            "trend": trend,
            "top_scopes": top_scopes,
            "by_model": by_model,
            "deny_reasons": deny_reasons,
        }

    async def subject_name_map(
        self, user_ids: Iterable[str], group_ids: Iterable[str]
    ) -> dict[str, str]:
        """批量取展示名：``{scope_id: 名称}``。

        好友 = 备注优先、其次昵称；群 = 群名。给总览榜 / 用量明细的「对象」列用
        —— 只有一串 QQ 号 / 群号看不出是谁。查不到的 id 不进结果，前端回退显示 id。
        """
        out: dict[str, str] = {}
        uids = sorted({str(u).strip() for u in user_ids if str(u or "").strip()})
        gids = sorted({str(g).strip() for g in group_ids if str(g or "").strip()})
        db = self._conn()
        if uids:
            marks = ",".join("?" * len(uids))
            async with db.execute(
                f"SELECT uin, COALESCE(NULLIF(remark, ''), NULLIF(nickname, '')) AS name "
                f"FROM friend_cache WHERE uin IN ({marks})",
                uids,
            ) as cur:
                for r in await cur.fetchall():
                    if r["name"]:
                        out.setdefault(str(r["uin"]), str(r["name"]))
        if gids:
            marks = ",".join("?" * len(gids))
            async with db.execute(
                f"SELECT group_id, name FROM group_cache WHERE group_id IN ({marks})", gids
            ) as cur:
                for r in await cur.fetchall():
                    if r["name"]:
                        out.setdefault(str(r["group_id"]), str(r["name"]))
        return out

    async def usage_sums(self, column: str, from_ts: int, to_ts: int) -> dict[str, int]:
        """按某个维度列聚合区间内的 token 总量，返回 ``{值: tokens}``。

        ``column`` 只允许白名单内的固定列名（防注入）：``sender_id`` / ``group_id`` / ``scope_id``。
        用于列表页「今日用量」排序与展示。
        """
        if column not in ("sender_id", "group_id", "scope_id"):
            raise ValueError(f"不支持的聚合列: {column}")
        async with self._conn().execute(
            f"""SELECT {column} AS k,
                       COALESCE(SUM(tok_in_other + tok_in_cached + tok_out), 0) AS tokens
                FROM usage_log
                WHERE ts >= ? AND ts <= ? AND {column} <> ''
                GROUP BY {column}""",
            (int(from_ts), int(to_ts)),
        ) as cur:
            return {str(r["k"]): int(r["tokens"] or 0) for r in await cur.fetchall()}

    async def subject_stats(self, subject_type: str, subject_id: str, from_ts: int, to_ts: int) -> dict[str, Any]:
        """单个对象（人 / 群）在区间内的用量总览与按天曲线。

        - ``user``：按 ``sender_id`` 聚合（这样该用户在**任意群**里的用量都能算进来）
        - ``group``：按 ``group_id`` 聚合
        """
        col = "group_id" if subject_type == "group" else "sender_id"
        where = f"WHERE {col} = ? AND ts >= ? AND ts <= ?"
        args = [str(subject_id), int(from_ts), int(to_ts)]
        db = self._conn()

        async with db.execute(
            f"""SELECT COUNT(*) AS events,
                       SUM(CASE WHEN kind = 'llm' THEN 1 ELSE 0 END) AS calls,
                       SUM(CASE WHEN status = 'denied' THEN 1 ELSE 0 END) AS denied,
                       COALESCE(SUM(tok_in_other), 0)  AS tok_in_other,
                       COALESCE(SUM(tok_in_cached), 0) AS tok_in_cached,
                       COALESCE(SUM(tok_out), 0)       AS tok_out,
                       SUM(estimated)                  AS estimated,
                       COALESCE(AVG(CASE WHEN latency_ms > 0 THEN latency_ms END), 0) AS avg_latency
                FROM usage_log {where}""",
            args,
        ) as cur:
            totals = dict(await cur.fetchone() or {})
        totals["tok_total"] = (
            int(totals.get("tok_in_other") or 0)
            + int(totals.get("tok_in_cached") or 0)
            + int(totals.get("tok_out") or 0)
        )

        async with db.execute(
            f"""SELECT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS day,
                       COALESCE(SUM(tok_in_other + tok_in_cached + tok_out), 0) AS tokens,
                       SUM(CASE WHEN kind = 'llm' THEN 1 ELSE 0 END) AS calls,
                       SUM(CASE WHEN status = 'denied' THEN 1 ELSE 0 END) AS denied
                FROM usage_log {where}
                GROUP BY day ORDER BY day""",
            args,
        ) as cur:
            series = [dict(r) for r in await cur.fetchall()]

        async with db.execute(
            f"""SELECT COALESCE(NULLIF(model, ''), '(未知)') AS model,
                       COALESCE(SUM(tok_in_other + tok_in_cached + tok_out), 0) AS tokens
                FROM usage_log {where} AND kind = 'llm'
                GROUP BY model ORDER BY tokens DESC LIMIT 10""",
            args,
        ) as cur:
            by_model = [dict(r) for r in await cur.fetchall()]

        return {"totals": totals, "series": series, "by_model": by_model}

    async def get_friend(self, uin: str, platform_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        """取单个好友缓存行（同一个人在多个平台下都有缓存时取最近同步的那行）。"""
        sql = "SELECT * FROM friend_cache WHERE uin = ?"
        args: list[Any] = [str(uin)]
        if platform_id:
            sql += " AND platform_id = ?"
            args.append(platform_id)
        async with self._conn().execute(sql + " ORDER BY updated_at DESC, rowid DESC LIMIT 1", args) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_group(self, group_id: str, platform_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        """取单个群缓存行（同一个群在多个平台下都有缓存时取最近同步的那行）。"""
        sql = "SELECT * FROM group_cache WHERE group_id = ?"
        args: list[Any] = [str(group_id)]
        if platform_id:
            sql += " AND platform_id = ?"
            args.append(platform_id)
        async with self._conn().execute(sql + " ORDER BY updated_at DESC, rowid DESC LIMIT 1", args) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_quotas_of(self, scope_type: str, scope_id: str) -> list[dict[str, Any]]:
        """取某个对象的所有周期额度。"""
        async with self._conn().execute(
            "SELECT * FROM llm_quota WHERE scope_type = ? AND scope_id = ? ORDER BY period",
            (scope_type, str(scope_id)),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def purge_old_usage(self, retention_days: int) -> int:
        """清理超期明细；``retention_days<=0`` 表示永久保留。返回删除行数。"""
        if retention_days <= 0:
            return 0
        cutoff = now_ts() - retention_days * 86400
        db = self._conn()
        cur = await db.execute("DELETE FROM usage_log WHERE ts < ?", (cutoff,))
        await db.commit()
        return cur.rowcount or 0

    # ------------------------------------------------------------------ #
    # 好友 / 群缓存
    # ------------------------------------------------------------------ #
    async def upsert_friends(self, platform_id: str, items: Iterable[dict[str, Any]]) -> int:
        """整体覆盖式写入好友缓存（同一平台）。返回写入条数。"""
        rows = [
            (
                platform_id,
                str(it.get("user_id") or it.get("uin") or "").strip(),
                str(it.get("nickname") or "").strip(),
                str(it.get("remark") or "").strip(),
                1,
                now_ts(),
            )
            for it in items
            if str(it.get("user_id") or it.get("uin") or "").strip()
        ]
        if not rows:
            return 0
        db = self._conn()
        await db.executemany(
            "INSERT INTO friend_cache(platform_id, uin, nickname, remark, is_friend, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(platform_id, uin) DO UPDATE SET "
            "nickname = excluded.nickname, remark = excluded.remark, "
            "is_friend = excluded.is_friend, updated_at = excluded.updated_at",
            rows,
        )
        await db.commit()
        return len(rows)

    async def list_friends(
        self,
        platform_id: Optional[str] = None,
        keyword: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """好友列表（昵称/备注模糊搜索），返回 ``{total, rows}``。

        **按 QQ 去重**（v1.5.2）：同一个人可能在不同 ``platform_id`` 下各存一份
        （换过 OneBot 适配器后 id 会变、或同时挂了两个机器人），而控制台列表
        不分平台 —— 不去重就会把同一个人显示成好几行。这里按 ``uin`` 只保留
        「最近一次同步」的那行（同一秒内按写入顺序取后写的那条）。
        """
        where, args = self._cache_where(platform_id, keyword, ("uin", "nickname", "remark"))
        dedup = (
            "WITH d AS (SELECT platform_id, uin, nickname, remark, is_friend, updated_at, "
            "ROW_NUMBER() OVER (PARTITION BY uin ORDER BY updated_at DESC, rowid DESC) AS __rn "
            f"FROM friend_cache{where}) "
        )
        db = self._conn()
        async with db.execute(dedup + "SELECT COUNT(*) AS c FROM d WHERE __rn = 1", args) as cur:
            row = await cur.fetchone()
            total = int(row["c"]) if row else 0
        async with db.execute(
            dedup
            + "SELECT platform_id, uin, nickname, remark, is_friend, updated_at FROM d "
            "WHERE __rn = 1 ORDER BY updated_at DESC, uin LIMIT ? OFFSET ?",
            [*args, int(limit), int(offset)],
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        return {"total": total, "rows": rows}

    async def upsert_groups(self, platform_id: str, items: Iterable[dict[str, Any]]) -> int:
        """整体覆盖式写入群缓存（同一平台）。返回写入条数。"""
        rows = [
            (
                platform_id,
                str(it.get("group_id") or "").strip(),
                str(it.get("group_name") or it.get("name") or "").strip(),
                int(it.get("member_count") or 0),
                int(it.get("max_member_count") or 0),
                str(it.get("owner") or "").strip(),
                now_ts(),
            )
            for it in items
            if str(it.get("group_id") or "").strip()
        ]
        if not rows:
            return 0
        db = self._conn()
        await db.executemany(
            "INSERT INTO group_cache(platform_id, group_id, name, member_count, max_member_count, owner, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(platform_id, group_id) DO UPDATE SET "
            "name = excluded.name, member_count = excluded.member_count, "
            "max_member_count = excluded.max_member_count, owner = excluded.owner, "
            "updated_at = excluded.updated_at",
            rows,
        )
        await db.commit()
        return len(rows)

    async def list_groups(
        self,
        platform_id: Optional[str] = None,
        keyword: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """群列表（群名/群号模糊搜索），返回 ``{total, rows}``。

        **按群号去重**（v1.5.2，与 ``list_friends`` 同一个坑）：同一个群可能挂在
        多个 ``platform_id`` 下，控制台列表不分平台，不去重就会重复出现。
        """
        where, args = self._cache_where(platform_id, keyword, ("group_id", "name"))
        dedup = (
            "WITH d AS (SELECT platform_id, group_id, name, member_count, max_member_count, "
            "owner, updated_at, ROW_NUMBER() OVER "
            "(PARTITION BY group_id ORDER BY updated_at DESC, rowid DESC) AS __rn "
            f"FROM group_cache{where}) "
        )
        db = self._conn()
        async with db.execute(dedup + "SELECT COUNT(*) AS c FROM d WHERE __rn = 1", args) as cur:
            row = await cur.fetchone()
            total = int(row["c"]) if row else 0
        async with db.execute(
            dedup
            + "SELECT platform_id, group_id, name, member_count, max_member_count, owner, updated_at "
            "FROM d WHERE __rn = 1 ORDER BY updated_at DESC, group_id LIMIT ? OFFSET ?",
            [*args, int(limit), int(offset)],
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        return {"total": total, "rows": rows}

    @staticmethod
    def _cache_where(
        platform_id: Optional[str],
        keyword: str,
        search_cols: tuple[str, ...],
    ) -> tuple[str, list[Any]]:
        parts: list[str] = []
        args: list[Any] = []
        if platform_id:
            parts.append("platform_id = ?")
            args.append(platform_id)
        kw = (keyword or "").strip()
        if kw:
            like = f"%{kw}%"
            parts.append("(" + " OR ".join(f"{c} LIKE ?" for c in search_cols) + ")")
            args.extend([like] * len(search_cols))
        return (" WHERE " + " AND ".join(parts)) if parts else "", args

    # ------------------------------------------------------------------ #
    # 规则备份 / 迁移（导出、导入）
    # ------------------------------------------------------------------ #
    # 参与备份的表（都只存「规则」；用量、明细、审计、成员缓存、头像不在其中）
    _RULE_TABLES = ("policy", "quota_level", "subject_level", "llm_quota")

    async def _all_rows(self, table: str) -> list[dict[str, Any]]:
        """整表读取（``table`` 只允许来自 :attr:`_RULE_TABLES` 白名单，防注入）。"""
        if table not in self._RULE_TABLES:
            raise ValueError(f"不允许读取的表：{table}")
        async with self._conn().execute(f"SELECT * FROM {table}") as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def export_rules(self) -> dict[str, Any]:
        """导出全部**规则类**配置：权限 / 等级 / 归级 / 额度。

        刻意**不导出**用量、明细、审计、群成员缓存：它们是历史数据，
        跟着规则一起搬没意义，还会让文件变得很大（要连历史一起备份就直接备份 ``.db`` 文件）。
        """
        return {
            "policies": await self.list_policies(),
            "levels": await self._all_rows("quota_level"),
            "subject_levels": await self._all_rows("subject_level"),
            "quotas": await self._all_rows("llm_quota"),
        }

    async def import_rules(self, data: Mapping[str, Any], mode: str = "merge") -> dict[str, Any]:
        """导入规则；``mode='merge'``（默认）只覆盖文件里出现的条目，``'replace'`` 先清空再导入。

        ⚠️ **等级 id 必须重映射**：``quota_level.id`` 是自增主键，换一台机器导入时
        必然与本地已有的 id 撞车，所以这里按 ``(kind, name)`` 找到/新建等级拿到**新 id**，
        再把「对象归级」与「等级额度」里引用的旧 id 改写成新 id。
        少了这一步，导入后归级和等级额度会全部指向别人的等级（而且不报错）。

        非法行会被跳过并计数（``skipped``），不会让整次导入失败 ——
        导入是「尽量恢复」，遇到一条坏数据就全盘不导入反而更糟。

        Returns:
            ``{mode, levels, subject_levels, quotas, policies, skipped}``
        """
        mode = "replace" if str(mode or "").lower() == "replace" else "merge"
        db = self._conn()
        stats = {"mode": mode, "levels": 0, "subject_levels": 0, "quotas": 0, "policies": 0, "skipped": 0}

        # 原子性（v1.0.0）：upsert_level / set_subject_level / upsert_quota / set_policy
        # 的正常路径各自 commit；导入期间把 db.commit 压成「静默」，最后统一提交一次。
        # 否则 replace 模式先删后导，中途失败会留下「旧规则已清空、新规则只进一半」
        # ——对迁移场景就是数据丢失。
        real_commit = db.commit

        async def _noop_commit() -> None:
            return None

        db.commit = _noop_commit  # type: ignore[method-assign]
        try:
            if mode == "replace":
                # 先清空这四张表（不动用量 / 日志 / 成员 / 最后消息）
                await db.execute("DELETE FROM subject_level")
                await db.execute("DELETE FROM llm_quota")
                await db.execute("DELETE FROM quota_level")
                await db.execute("DELETE FROM policy")

            # 1) 等级：按 (kind, name) upsert，并记下 旧id → 新id
            id_map: dict[str, int] = {}
            for lv in _as_list(data.get("levels")):
                kind = str(lv.get("kind") or "").strip()
                name = str(lv.get("name") or "").strip()
                if kind not in ("user", "group") or not name:
                    stats["skipped"] += 1
                    continue
                new_id = await self.upsert_level(
                    kind,
                    name,
                    description=str(lv.get("description") or ""),
                    effect=_norm_effect(lv.get("effect")),
                    command_effect=_norm_effect(lv.get("command_effect")),
                    effect_group=_norm_effect(lv.get("effect_group")),
                    command_effect_group=_norm_effect(lv.get("command_effect_group")),
                    sort_order=_as_int(lv.get("sort_order"), 0),
                    provider_id=str(lv.get("provider_id") or ""),
                    fallback_provider_id=str(lv.get("fallback_provider_id") or ""),
                    # v9：可切换模型列表（备份里是 list 或 JSON 文本，两种都收）
                    switch_providers=lv.get("switch_providers"),
                    # v10：允许切换的开关（备份里可能是 true / "1" / 0，统一收口）
                    switch_enabled=lv.get("switch_enabled"),
                )
                old_id = lv.get("id")
                if old_id is not None:
                    id_map[str(old_id)] = int(new_id)
                stats["levels"] += 1

            def _map_level(raw: Any) -> Optional[int]:
                """把旧的等级 id 换算成导入后的新 id；映射不到就返回 None（跳过该行）。"""
                if raw in (None, ""):
                    return None
                got = id_map.get(str(raw))
                return int(got) if got else None

            # 2) 对象归级
            for row in _as_list(data.get("subject_levels")):
                scope_type = str(row.get("scope_type") or "").strip()
                scope_id = str(row.get("scope_id") or "").strip()
                level_id = _map_level(row.get("level_id"))
                if scope_type not in ("user", "group") or not scope_id or not level_id:
                    stats["skipped"] += 1
                    continue
                await self.set_subject_level(scope_type, scope_id, level_id)
                stats["subject_levels"] += 1

            # 3) 额度（level 维度的 scope_id 同样要换成新 id）
            for row in _as_list(data.get("quotas")):
                scope_type = str(row.get("scope_type") or "").strip()
                scope_id = str(row.get("scope_id") or "").strip()
                period = str(row.get("period") or "").strip()
                if scope_type not in ("user", "group", "member", "level", "global"):
                    stats["skipped"] += 1
                    continue
                if period not in PERIODS:
                    stats["skipped"] += 1
                    continue
                if scope_type == "level":
                    mapped = _map_level(scope_id)
                    if mapped is None:
                        stats["skipped"] += 1
                        continue
                    scope_id = str(mapped)
                elif scope_type == "global":
                    # v1.4.0：全局额度分私聊 / 群聊（scope_id = private / group）。
                    # 旧备份是 '*'（或空）→ 展开成两份都写入，旧备份导入后行为不变。
                    scene_id = str(row.get("scope_id") or "*").strip()
                    if scene_id in ("", "*"):
                        global_scenes: tuple[str, ...] = ("private", "group")
                    elif scene_id in ("private", "group"):
                        global_scenes = (scene_id,)
                    else:
                        stats["skipped"] += 1
                        continue
                elif not scope_id:
                    stats["skipped"] += 1
                    continue
                # mode / reset_at 不透传原始值（v1.0.0）：坏 mode 会被当成 enforce、
                # 字符串 reset_at 写进 INTEGER 列会让「重置到期」永不命中 → 计数永不清零。
                q_mode = str(row.get("mode") or "enforce").strip().lower()
                if q_mode not in ("enforce", "observe"):
                    q_mode = "enforce"
                try:
                    reset_at = (
                        int(row.get("reset_at")) if row.get("reset_at") not in (None, "") else None
                    )
                except (TypeError, ValueError):
                    reset_at = None
                # 全局额度按迁移出的场景列表写（'*' → private + group 两行）
                target_scenes = global_scenes if scope_type == "global" else (scope_id,)
                for sid in target_scenes:
                    await self.upsert_quota(
                        scope_type,
                        sid,
                        period,
                        _as_int(row.get("limit_tokens"), 0),
                        mode=q_mode,
                        reset_at=reset_at,
                    )
                    stats["quotas"] += 1

            # 4) 权限规则（LLM / 指令 / 群成员 / 模型类；scene 一并带上）
            # 校验口径与 /policy 接口对齐（v1.0.0）：feature 白名单、member 的 scope_id
            # 必须是「群号:QQ」、global 不收 LLM 规则——收下这些只会变成死数据。
            # v9：model / model_choice 这两类 feature 的 effect 存的是**提供商 id**，
            # 不能过 _norm_effect（会被抹成 inherit = 凭空丢掉「专属模型 / 用户选择」）。
            for row in _as_list(data.get("policies")):
                scope_type = str(row.get("scope_type") or "").strip()
                scope_id = str(row.get("scope_id") or "").strip()
                feature = str(row.get("feature") or "llm").strip() or "llm"
                scene = str(row.get("scene") or "").strip()
                if scope_type not in ("user", "group", "member", "global"):
                    stats["skipped"] += 1
                    continue
                if feature.startswith("command:") and not feature[len("command:"):].strip():
                    stats["skipped"] += 1
                    continue
                if (
                    feature not in ("llm", "command")
                    and feature not in PROVIDER_FEATURES
                    and not feature.startswith("command:")
                ):
                    stats["skipped"] += 1
                    continue
                if scope_type == "global" and feature == "llm":
                    stats["skipped"] += 1
                    continue
                if feature in PROVIDER_FEATURES:
                    # 模型类规则挂「好友 / 群」两层（v1.3.12 起群也有专属模型，
                    # 与 /subject/model 接口的 type 参数一致）；member / global 不收。
                    raw_effect = str(row.get("effect") or "").strip()
                    if scope_type not in ("user", "group") or not raw_effect or raw_effect == "inherit":
                        stats["skipped"] += 1
                        continue
                    await self.set_policy(
                        scope_type, scope_id, raw_effect, feature=feature,
                        note=str(row.get("note") or ""), scene=scene,
                    )
                    stats["policies"] += 1
                    continue
                if scope_type != "user":
                    scene = ""
                if scope_type == "global":
                    scope_id = "*"
                if scope_type == "member":
                    mgid, muid = parse_member_scope_id(scope_id)
                    if not (mgid and muid):
                        stats["skipped"] += 1
                        continue
                    scope_id = member_scope_id(mgid, muid)
                if not scope_id or scene not in ("", "private", "group"):
                    stats["skipped"] += 1
                    continue
                await self.set_policy(
                    scope_type,
                    scope_id,
                    _norm_effect(row.get("effect")),
                    feature=feature,
                    note=str(row.get("note") or ""),
                    scene=scene,
                )
                stats["policies"] += 1

            await real_commit()  # 整个导入一次提交（上面各步骤的 commit 已被静默）
            return stats
        except Exception:
            await db.rollback()  # 中途失败 → 全部回滚，绝不留「清空了旧规则、只导了一半」的中间态
            raise
        finally:
            db.commit = real_commit


    # ------------------------------------------------------------------ #
    # 群成员缓存（v7：群成员级管控用）
    # ------------------------------------------------------------------ #
    async def upsert_group_members(
        self,
        platform_id: str,
        group_id: str,
        items: Iterable[dict[str, Any]],
    ) -> int:
        """整体覆盖式写入某群的成员缓存，返回写入条数。

        覆盖式 = 先删掉该群旧成员再写入（协议端退群的人在本地也应消失）。
        **不动 policy 表**：成员退了但规则还留着是更安全的默认（回来即生效）。
        """
        gid = str(group_id or "").strip()
        if not gid:
            return 0
        rows = [
            (
                platform_id,
                gid,
                str(it.get("user_id") or "").strip(),
                str(it.get("nickname") or "").strip(),
                str(it.get("card") or "").strip(),
                str(it.get("role") or "").strip(),
                str(it.get("level") or "").strip(),
                int(it.get("joined_at") or it.get("join_time") or 0),
                now_ts(),
            )
            for it in items
            if str(it.get("user_id") or "").strip()
        ]
        db = self._conn()
        await db.execute("DELETE FROM group_member WHERE group_id = ?", (gid,))
        if rows:
            await db.executemany(
                "INSERT INTO group_member(platform_id, group_id, user_id, nickname, card, role, "
                "level, joined_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(platform_id, group_id, user_id) DO UPDATE SET "
                "nickname = excluded.nickname, card = excluded.card, role = excluded.role, "
                "level = excluded.level, joined_at = excluded.joined_at, "
                "updated_at = excluded.updated_at",
                rows,
            )
        await db.commit()
        return len(rows)

    async def group_members_synced_at(self, group_id: str) -> int:
        """某群成员缓存的最后同步时间（该群**全量**成员的最大 updated_at）。

        不用分页结果的 max()：size < total 时那只是「当前页里最新的一条」，
        显示出来的同步时间会偏旧。
        """
        gid = str(group_id or "").strip()
        if not gid:
            return 0
        async with self._conn().execute(
            "SELECT COALESCE(MAX(updated_at), 0) FROM group_member WHERE group_id = ?",
            (gid,),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0] or 0) if row else 0

    async def list_group_members(
        self,
        group_id: str,
        keyword: str = "",
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        """某群的成员列表（昵称 / 群名片 / QQ 模糊搜索），返回 ``{total, rows}``。

        排序：群主 → 管理员 → 普通成员，同级按群名片/昵称。
        """
        where, args = self._cache_where(None, keyword, ("user_id", "nickname", "card"))
        where = (where + " AND " if where else " WHERE ") + "group_id = ?"
        args = [*args, str(group_id)]
        order = (
            "CASE role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, "
            "COALESCE(NULLIF(card, ''), nickname), user_id"
        )
        db = self._conn()
        async with db.execute(f"SELECT COUNT(*) AS c FROM group_member{where}", args) as cur:
            row = await cur.fetchone()
            total = int(row["c"]) if row else 0
        async with db.execute(
            f"SELECT * FROM group_member{where} ORDER BY {order} LIMIT ? OFFSET ?",
            [*args, int(limit), int(offset)],
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        return {"total": total, "rows": rows}

    async def group_member_counts(self) -> dict[str, int]:
        """每个群已缓存多少成员 ``{group_id: n}``（列表页展示「成员已同步 N 人」）。"""
        async with self._conn().execute(
            "SELECT group_id, COUNT(*) AS c FROM group_member GROUP BY group_id"
        ) as cur:
            return {str(r["group_id"]): int(r["c"]) for r in await cur.fetchall()}

    async def get_group_member(self, group_id: str, user_id: str) -> Optional[dict[str, Any]]:
        async with self._conn().execute(
            "SELECT * FROM group_member WHERE group_id = ? AND user_id = ?",
            (str(group_id), str(user_id)),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def usage_sums_in_group(
        self,
        group_id: str,
        from_ts: int,
        to_ts: int,
    ) -> dict[str, int]:
        """某群内**按发言人**聚合的 token 用量 ``{sender_id: tokens}``（成员列表展示用）。

        与 :meth:`usage_sums` 的区别：那个按 `sender_id` 跨群汇总，
        这个只在指定群里统计，才能回答「这个人在**这个群**用了多少」。
        """
        async with self._conn().execute(
            """SELECT sender_id, COALESCE(SUM(tok_in_other + tok_in_cached + tok_out), 0) AS tokens
               FROM usage_log
               WHERE group_id = ? AND ts >= ? AND ts <= ? AND sender_id <> ''
               GROUP BY sender_id""",
            (str(group_id), int(from_ts), int(to_ts)),
        ) as cur:
            return {str(r["sender_id"]): int(r["tokens"] or 0) for r in await cur.fetchall()}

    # ------------------------------------------------------------------ #
    # 审计
    # ------------------------------------------------------------------ #
    async def log_audit(self, actor: str, action: str, payload: str = "") -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO audit_log(ts, actor, action, payload) VALUES(?, ?, ?, ?)",
            (now_ts(), actor, action, payload),
        )
        await db.commit()

    async def list_audit(self, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        db = self._conn()
        async with db.execute("SELECT COUNT(*) AS c FROM audit_log") as cur:
            row = await cur.fetchone()
            total = int(row["c"]) if row else 0
        async with db.execute(
            "SELECT * FROM audit_log ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            (int(limit), int(offset)),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        return {"total": total, "rows": rows}
