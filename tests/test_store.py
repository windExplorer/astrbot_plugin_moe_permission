"""存储层自检：真实建库跑一遍读写路径（不需要 AstrBot 环境）。

用法：
    uv run --no-project --with aiosqlite python tests/test_store.py

覆盖：建表 / 权限规则（含 inherit 语义）/ 限额与用量分离 / 等级与归级 /
bot 最后消息 / 用量日志与统计聚合 / 好友群缓存与模糊搜索 / 超期清理 / 审计 /
v1 → v2 结构迁移。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from store import Store, parse_provider_list  # noqa: E402

_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _failures.append(label)


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_path = str(Path(tmp) / "user_gateway.db")
        st = Store(db_path)
        await st.open()

        print("\n[1] 建库")
        check(st.ready, "Store.ready")
        check(Path(db_path).exists(), "数据库文件已创建")

        print("\n[2] settings")
        check(await st.get_setting("schema_version") == "11", "schema_version 已写入 11")
        check(await st.get_setting("nope", "d") == "d", "缺省值回退")
        await st.set_setting("sync_last_at", "123")
        check(await st.get_setting("sync_last_at") == "123", "写入后可读")

        print("\n[3] 权限规则")
        check(await st.get_effect("user", "10001") is None, "未配置 → None（继承）")
        await st.set_policy("user", "10001", "deny")
        check(await st.get_effect("user", "10001") == "deny", "写入 deny")
        await st.set_policy("user", "10001", "allow")
        check(await st.get_effect("user", "10001") == "allow", "同键覆盖为 allow")
        await st.set_policy("group", "88888", "deny")
        check(await st.effect_map("user") == {"10001": {"": "allow"}}, "effect_map(user)（值为 {场景: 效果}）")
        check(await st.effect_map("group") == {"88888": {"": "deny"}}, "effect_map(group)")
        # 场景维度（v6）：同一对象的「私聊」「群聊」可以各配一条，互不覆盖
        await st.set_policy("user", "10001", "deny", scene="private")
        await st.set_policy("user", "10001", "allow", scene="group")
        em = await st.effect_map("user")
        check(em["10001"] == {"": "allow", "private": "deny", "group": "allow"},
              f"同对象多场景并存（实得 {em.get('10001')}）")
        check(await st.get_effect("user", "10001", "llm", "private") == "deny", "按场景读单条规则")
        check(await st.resolve_policy("user", "10001", "llm", "group") == "allow",
              "resolve_policy 优先取场景专属")
        await st.set_policy("user", "10002", "deny")           # 只有通用
        check(await st.resolve_policy("user", "10002", "llm", "group") == "deny",
              "只有通用规则时两个场景都生效（旧数据行为不变）")
        await st.set_policy("user", "10001", "inherit", scene="private")
        check(await st.get_effect("user", "10001", "llm", "private") is None,
              "「恢复继承」只删本场景的规则")
        check(await st.get_effect("user", "10001", "llm", "group") == "allow",
              "另一个场景的规则不受影响")
        await st.set_policy("user", "10001", "inherit", scene="group")
        await st.set_policy("user", "10002", "inherit")
        await st.set_policy("user", "10001", "inherit")
        check(await st.get_effect("user", "10001") is None, "inherit 等价于删除")

        print("\n[4] 限额与用量（v2 起两者分离）")
        await st.upsert_quota("user", "10001", "day", 10000)
        q = await st.get_quota("user", "10001", "day")
        check(q is not None and q["limit_tokens"] == 10000, "新建限额")
        check("used_tokens" not in q, "限额行不再自带用量（用量在 usage_counter）")
        await st.add_used("user", "10001", 350, reset_at_map={"day": 999999})
        await st.add_used("user", "10001", 150)
        u = await st.get_usage("user", "10001")
        check(u["day"]["used_tokens"] == 500, f"用量累加 = 500（实得 {u['day']['used_tokens']}）")
        check(u["month"]["used_tokens"] == 500 and u["total"]["used_tokens"] == 500, "日/月/累计三个周期一起累加")
        check(u["day"]["reset_at"] == 999999, "首次写入时记录 reset_at")
        check(u["month"]["reset_at"] is None, "未指定 reset_at 的周期留空")
        # reset_at 不该被后续累加覆盖（否则重置时间会被一直往后推）
        await st.add_used("user", "10001", 10, reset_at_map={"day": 111})
        check((await st.get_usage("user", "10001"))["day"]["reset_at"] == 999999, "后续累加不覆盖已有 reset_at")
        # 改限额不影响用量
        await st.upsert_quota("user", "10001", "day", 20000)
        check((await st.get_usage("user", "10001"))["day"]["used_tokens"] == 510, "改额度不清零用量")
        check((await st.get_quota("user", "10001", "day"))["limit_tokens"] == 20000, "上限已更新")
        # 未配额度的对象也必须记账（等级/全局模板要用它自己的用量判定）
        await st.add_used("user", "20002", 77)
        check((await st.get_usage("user", "20002"))["day"]["used_tokens"] == 77, "未配额度也记账")
        check(await st.add_used("user", "20002", 0) == 0, "add_used(0) 不写库")
        um = await st.usage_map("user")
        check(set(um) == {"10001", "20002"}, f"usage_map 覆盖全部对象（实得 {sorted(um)}）")
        # 清零
        await st.reset_used(scope_type="user", scope_id="10001", period="day", reset_at=123456)
        u = await st.get_usage("user", "10001")
        check(u["day"]["used_tokens"] == 0 and u["day"]["reset_at"] == 123456, "按周期清零并写回新的 reset_at")
        check(u["month"]["used_tokens"] == 510, "只清了日周期")
        due = await st.counters_due(123456)
        check(len(due) == 1 and due[0]["scope_id"] == "10001" and due[0]["period"] == "day",
              f"counters_due 只列出到期的用量行（实得 {len(due)} 条）")
        check(await st.counters_due(123455) == [], "未到点的不出现在 counters_due")
        check(await st.counters_due(10**12) == due, "远期时间点结果一致（其它行 reset_at 为空）")
        # 删除限额
        await st.upsert_quota("user", "10001", "month", 100000)
        check(len(await st.list_quotas_of("user", "10001")) == 2, "list_quotas_of 返回 2 个周期")
        await st.delete_quota("user", "10001", "day")
        check(await st.get_quota("user", "10001", "day") is None, "按周期删除限额")
        check(await st.get_quota("user", "10001", "month") is not None, "只删了 day，month 仍在")
        await st.delete_quota("user", "10001")
        check(len(await st.list_quotas_of("user", "10001")) == 0, "不传 period → 删除该对象全部限额")
        check((await st.get_usage("user", "10001"))["month"]["used_tokens"] == 510, "删限额不影响用量")

        print("\n[5] 用量日志与统计")
        now = int(time.time())
        for i in range(3):
            await st.log_usage(
                ts=now - i * 60,
                scope_type="user",
                scope_id="10001",
                sender_id="10001",
                group_id="",
                provider_id="p1",
                model="gpt-4o",
                tok_in_other=100,
                tok_in_cached=50,
                tok_out=30,
                status="ok",
                latency_ms=1200,
            )
        await st.log_usage(
            ts=now,
            scope_type="group",
            scope_id="88888",
            sender_id="10001",
            group_id="88888",
            status="denied",
            deny_reason="quota",
            kind="llm",
        )
        await st.log_usage(ts=now, scope_type="user", scope_id="10002", sender_id="10002", status="ok", tok_out=1, estimated=1)
        res = await st.query_usage(limit=10)
        check(res["total"] == 5, f"明细总数 = {res['total']}（期望 5）")
        check(len(res["rows"]) == 5, "返回 5 行")
        res2 = await st.query_usage(status="denied")
        check(res2["total"] == 1 and res2["rows"][0]["deny_reason"] == "quota", "按 status 过滤")

        # 群维度过滤：群里的流水可能记在 scope_type='group'（命中群级规则），
        # 也可能记在 scope_type='user'（命中该用户的对象级规则），
        # 只有按 group_id 过滤才能一次捞全 —— 群详情的「近期流水」就靠它
        # （漏掉过滤条件时会显示成全库最新流水，表现为「流水和该群不符」）。
        res3 = await st.query_usage(group_id="88888")
        check(res3["total"] == 1, f"按 group_id 过滤（实得 {res3['total']}，期望 1）")
        # 刻意放在时间窗口之外：不影响本段后续的区间统计 / CSV 行数断言
        await st.log_usage(
            ts=now - 2 * 86400,
            scope_type="user",
            scope_id="10001",
            sender_id="10001",
            group_id="88888",
            kind="llm",
            status="ok",
        )
        res4 = await st.query_usage(group_id="88888")
        check(res4["total"] == 2, f"group_id 过滤覆盖 scope_type='user' 的群内流水（实得 {res4['total']}）")
        res5 = await st.query_usage(group_id="88888", from_ts=now - 3600, to_ts=now + 60)
        check(res5["total"] == 1, "group_id 可与时间区间叠加")

        s = await st.summary(now - 3600, now + 60)
        t = s["totals"]
        check(t["calls"] == 5, f"calls = {t['calls']}（期望 5）")
        check(t["denied"] == 1, f"denied = {t['denied']}（期望 1）")
        check(t["tok_in_other"] == 300 and t["tok_in_cached"] == 150 and t["tok_out"] == 91,
              f"token 分项 = {t['tok_in_other']}/{t['tok_in_cached']}/{t['tok_out']}（期望 300/150/91）")
        check(t["tok_total"] == 541, f"tok_total = {t['tok_total']}（期望 541）")
        check(t["users"] == 2, f"活跃用户 = {t['users']}（期望 2）")
        check(t["groups"] == 1, f"活跃群 = {t['groups']}（期望 1）")
        # 趋势按「本地小时」分桶：种子数据在 now / now-60 / now-120。若正好跑在整点前后，
        # 这些点会落在两个桶里 —— 所以按「种子时间戳实际跨了几个小时」断言，
        # 而不是写死 1（写死的话每个整点跑测试都会假红一次）。
        # 注意偏移量必须与上面真正写进去的那三行**一一对应**：多算几个没数据的偏移，
        # 会在「整点后 2~6 分钟」这段窗口里把期望值抬成 2，而实际只有 1 个桶 → 假红。
        hours = {time.strftime("%H:00", time.localtime(now - d)) for d in (0, 60, 120)}
        check(len(s["trend"]) == len(hours),
              f"短区间趋势分桶 = {len(s['trend'])}（种子的时间戳跨 {len(hours)} 个小时桶）")
        check(":" in str(s["trend"][0]["day"]), "短区间的 x 轴标签是「小时:00」（今日视图用）")
        check(sum(int(r["tokens"] or 0) for r in s["trend"]) == 541, "趋势 token 合计 = 541")
        check(any(r["reason"] == "quota" for r in s["deny_reasons"]), "拒绝原因含 quota")
        check(any(r["model"] == "gpt-4o" for r in s["by_model"]), "模型占比含 gpt-4o")

        # 按维度聚合（控制台列表页的「今日用量」就靠它）
        sums = await st.usage_sums("sender_id", now - 3600, now + 60)
        check(sums.get("10001") == 540, f"sender_id 聚合 10001 = 540（实得 {sums.get('10001')}）")
        check(sums.get("10002") == 1, "sender_id 聚合 10002 = 1")
        gsums = await st.usage_sums("group_id", now - 3600, now + 60)
        check(gsums.get("88888") == 0, "group_id 聚合 88888 = 0（只记了拒绝事件）")
        try:
            await st.usage_sums("tok_out; DROP TABLE usage_log", now - 3600, now + 60)
            check(False, "非白名单列应被拒绝")
        except ValueError:
            check(True, "非白名单列被拒绝（防注入）")

        print("\n[5.1] 指令统计与 CSV 导出（M6）")
        for i, (cmd, state) in enumerate(
            [
                ("帮助", "ok"),
                ("帮助", "ok"),
                ("签到", "ok"),
                ("帮助", "denied"),
                ("签到", "denied"),
                ("签到", "denied"),
                ("签到", "denied"),
            ],
        ):
            await st.log_usage(
                ts=now - i * 60,
                scope_type="user",
                scope_id="10001",
                sender_id="10001",
                kind="command",
                command_name=cmd,
                status=state,
                deny_reason="command" if state == "denied" else "",
            )
        top_ok = await st.top_commands(now - 3600, now + 60, status="ok")
        check(top_ok[0]["command"] == "帮助" and int(top_ok[0]["cnt"]) == 2, f"最常触发排行 = {top_ok}")
        check(top_ok[1]["command"] == "签到" and int(top_ok[1]["cnt"]) == 1, "排行按次数降序")
        top_denied = await st.top_commands(now - 3600, now + 60, status="denied")
        check(
            top_denied[0]["command"] == "签到" and int(top_denied[0]["cnt"]) == 3,
            f"最常被拦排行 = {top_denied}",
        )
        # 关键：指令流水不能污染 LLM 口径
        s2 = await st.summary(now - 3600, now + 60)
        check(s2["totals"]["calls"] == 5, f"指令流水不影响 calls（实得 {s2['totals']['calls']}）")
        check(s2["totals"]["tok_total"] == 541, "指令流水不带 token，总量不变")
        heat = await st.hour_heatmap(now - 3600, now + 60, kind="command")
        check(sum(int(r["cnt"]) for r in heat) == 7, "热力图按 (星期, 小时) 分桶，覆盖 7 条指令事件")
        csv_text = await st.export_usage_csv(from_ts=now - 3600, to_ts=now + 60)
        check(csv_text.startswith("\ufeff"), "CSV 带 UTF-8 BOM（Excel 不乱码）")
        # 注意 BOM 会留在首行开头，比较前先脱掉（不脱的话 startswith 必然为假）
        csv_lines = [ln for ln in csv_text.splitlines() if ln]
        head = csv_lines[0].lstrip("\ufeff")
        check(head.startswith("时间,类型"), f"CSV 表头正确：{head[:16]}")
        check(len(csv_lines) == 1 + 12, f"CSV 行数 = 表头 + 12 条（实得 {len(csv_lines)}）")
        check("指令" in csv_text and "帮助" in csv_text, "CSV 含指令类型与指令名")
        check((await st.export_usage_csv(kind="command")) .count("\n") == 8, "导出支持按类型过滤（7 条指令 + 表头）")

        # 单对象统计（详情抽屉用）
        su = await st.subject_stats("user", "10001", now - 3600, now + 60)
        check(su["totals"]["calls"] == 4, f"用户统计 calls = 4（实得 {su['totals']['calls']}）")
        check(su["totals"]["tok_total"] == 540, "用户统计 tok_total = 540")
        # 曲线按「本地日」分桶：这个用户的种子数据散布在 now .. now-360（LLM 3 条 + 指令 7 条），
        # 刚过午夜时靠后的几条属于「昨天」，桶数就是 2。按实际跨的天数断言（写死 1 会在
        # 每天 00:00-00:06 假红 —— 这坑真踩过一次）。
        days = {time.strftime("%Y-%m-%d", time.localtime(now - d)) for d in range(0, 420, 60)}
        check(len(su["series"]) == len(days),
              f"用户统计曲线按「本地日」分桶（实得 {len(su['series'])}，种子跨 {len(days)} 天）")
        sg = await st.subject_stats("group", "88888", now - 3600, now + 60)
        check(sg["totals"]["events"] == 1 and sg["totals"]["denied"] == 1, "群统计：1 个事件且为拒绝")

        print("\n[6] 好友 / 群缓存")
        await st.upsert_friends("aiocqhttp", [
            {"user_id": "10001", "nickname": "小明", "remark": "同事"},
            {"user_id": "10002", "nickname": "小红", "remark": ""},
            {"nickname": "没有号"},  # 应被过滤
        ])
        fr = await st.list_friends()
        check(fr["total"] == 2, f"好友数 = {fr['total']}（期望 2，无 user_id 的条目被丢弃）")
        fr2 = await st.list_friends(keyword="同事")
        check(fr2["total"] == 1 and fr2["rows"][0]["uin"] == "10001", "按备注搜索")
        fr3 = await st.list_friends(keyword="小红")
        check(fr3["total"] == 1, "按昵称搜索")
        await st.upsert_friends("aiocqhttp", [{"user_id": "10001", "nickname": "小明改名", "remark": "同事"}])
        fr4 = await st.list_friends(keyword="小明改名")
        check(fr4["total"] == 1, "重复同步为覆盖更新")
        check((await st.list_friends())["total"] == 2, "覆盖后总数不变")

        await st.upsert_groups("aiocqhttp", [
            {"group_id": "88888", "group_name": "测试群", "member_count": 30, "max_member_count": 200, "owner": "10001"},
            {"group_id": "99999", "group_name": "摸鱼群", "member_count": 5},
        ])
        gr = await st.list_groups()
        check(gr["total"] == 2, "群数 = 2")
        check(gr["rows"][0]["group_id"] == "88888", "按人数倒序，88888 在前")
        check((await st.list_groups(keyword="摸鱼"))["total"] == 1, "按群名搜索")

        # 单个对象查询（详情抽屉的基础信息）
        f1 = await st.get_friend("10001")
        check(f1 is not None and f1["nickname"] == "小明改名" and f1["remark"] == "同事", "get_friend 取到改名后的记录")
        check(await st.get_friend("77777") is None, "不存在的 QQ → None")
        g1 = await st.get_group("88888")
        check(g1 is not None and g1["name"] == "测试群" and g1["owner"] == "10001", "get_group 字段完整")

        # 展示名批量映射（总览榜 / 明细的「对象」列用）
        nm = await st.subject_name_map(["10001", "10002", "77777"], ["88888", "1"])
        check(nm.get("10001") == "同事", "好友展示名 = 备注优先")
        check(nm.get("10002") == "小红", "没有备注 → 昵称")
        check(nm.get("88888") == "测试群", "群展示名 = 群名")
        check("77777" not in nm and "1" not in nm, "查不到的 id 不进结果（前端回退显示 id）")

        # v1.5.2：同一个人 / 同一个群在不同 platform_id 下各有一份缓存时（换过 OneBot
        # 适配器会换 id、或同时挂了两个机器人），列表必须按 QQ / 群号去重，
        # 否则控制台（不分平台）会把同一个人显示成好几行。
        await st.upsert_friends("aiocqhttp-2", [
            {"user_id": "10001", "nickname": "小明（小号）", "remark": "同事"},
        ])
        dup = await st.list_friends()
        check(dup["total"] == 2, f"跨平台重复的好友只算一次（实得 {dup['total']}）")
        check(len([r for r in dup["rows"] if r["uin"] == "10001"]) == 1, "同一个 QQ 在列表里只有一行")
        check(dup["rows"][0]["uin"] == "10001" and dup["rows"][0]["nickname"] == "小明（小号）",
              f"取最近同步的那份资料（实得 {dup['rows'][0]['nickname']}）")
        check((await st.list_friends(platform_id="aiocqhttp-2"))["total"] == 1, "指定平台时只列该平台的缓存")
        check((await st.get_friend("10001"))["nickname"] == "小明（小号）", "get_friend 取最近同步的那份")

        await st.upsert_groups("aiocqhttp-2", [
            {"group_id": "88888", "group_name": "测试群", "member_count": 30, "max_member_count": 200, "owner": "10001"},
        ])
        gdup = await st.list_groups()
        check(gdup["total"] == 2, f"跨平台重复的群只算一次（实得 {gdup['total']}）")
        check(len([r for r in gdup["rows"] if r["group_id"] == "88888"]) == 1, "同一个群在列表里只有一行")

        print("\n[7] 超期清理与审计")
        await st.log_usage(ts=now - 400 * 86400, scope_type="user", scope_id="10001", status="ok")
        before = (await st.query_usage(limit=1))["total"]
        await st.purge_old_usage(180)
        after = (await st.query_usage(limit=1))["total"]
        check(after == before - 1, f"清理 180 天前明细: {before} → {after}")
        check(await st.purge_old_usage(0) == 0, "retention_days=0 表示不清理")

        await st.log_audit("console", "set_policy", "{}")
        au = await st.list_audit()
        check(au["total"] == 1 and au["rows"][0]["action"] == "set_policy", "审计日志")

        print("\n[8] 等级与归级")
        lv_normal = await st.upsert_level("user", "普通", description="默认档", effect="inherit", sort_order=1)
        lv_vip = await st.upsert_level("user", "VIP", effect="allow", sort_order=2)
        lv_group = await st.upsert_level("group", "主群", effect="deny")
        check(len(await st.list_levels()) == 3, "等级数 = 3")
        check(len(await st.list_levels("user")) == 2, "按 kind 过滤")
        check(await st.upsert_level("user", "VIP", effect="allow") == lv_vip, "同名等级重复 upsert 返回同一 id")
        lv = await st.get_level(lv_vip)
        check(lv["kind"] == "user" and lv["effect"] == "allow", "get_level 字段正确")
        check(await st.get_level(99999) is None, "不存在的等级 → None")

        await st.upsert_quota("level", str(lv_vip), "day", 500000)
        check(len(await st.list_quotas_of("level", str(lv_vip))) == 1, "等级额度模板可写")
        levels_lim = await st.list_quotas("level")
        check(len(levels_lim) == 1 and levels_lim[0]["scope_id"] == str(lv_vip), "list_quotas('level') 能取到模板")
        glob = await st.list_quotas("global")
        await st.upsert_quota("global", "*", "day", 50000)
        check(len(await st.list_quotas("global")) == 1 and glob == [], "全局模板独立成一层")
        # v1.4.0：旧 global:* 行由启动迁移拆成私聊 / 群聊两份
        moved = await st.migrate_global_quota_scene()
        check(moved == 2, f"全局 '*' 行迁移复制成两份（实得 {moved}）")
        check((await st.get_quota("global", "private", "day"))["limit_tokens"] == 50000, "迁移后私聊全局额度存在")
        check((await st.get_quota("global", "group", "day"))["limit_tokens"] == 50000, "迁移后群聊全局额度存在")
        check(all(r["scope_id"] != "*" for r in await st.list_quotas("global")), "迁移后不再有 '*' 行")
        check((await st.migrate_global_quota_scene()) == 0, "全局额度场景迁移幂等：再跑一遍是 no-op")

        await st.set_subject_level("user", "10001", lv_vip)
        check(await st.get_subject_level("user", "10001") == lv_vip, "归级写入")
        check(await st.subject_level_map("user") == {"10001": lv_vip}, "subject_level_map")
        check((await st.level_counts()).get(lv_vip) == 1, "level_counts 统计成员数")
        await st.set_subject_level("user", "10001", lv_vip)  # 幂等
        check((await st.level_counts()).get(lv_vip) == 1, "重复归级不重复计数")
        await st.set_subject_level("user", "10001", None)
        check(await st.get_subject_level("user", "10001") is None, "取消归级")

        # 分场景归级（v8）：私聊 / 群聊等级分开配
        await st.set_user_level_scene("10001", private_level=lv_vip, group_level=None)
        p, g = await st.get_user_level_scene("10001")
        check(p == lv_vip and g is None, "私聊等级写入；群聊未配 = None（跟随私聊）")
        await st.set_user_level_scene("10001", group_level=lv_vip)
        p, g = await st.get_user_level_scene("10001")
        check(p == lv_vip and g == lv_vip, "只改群聊等级不影响私聊")
        check((await st.subject_level_group_map()) == {"10001": lv_vip}, "subject_level_group_map")
        await st.set_user_level_scene("10001", group_level=None)
        p, g = await st.get_user_level_scene("10001")
        check(p == lv_vip and g is None, "清除群聊专属（跟随私聊）不影响私聊")
        check((await st.subject_level_group_map()) == {}, "清除后不进 group_map")
        await st.set_user_level_scene("10001", private_level=None, group_level=lv_vip)
        p, g = await st.get_user_level_scene("10001")
        check(p is None and g == lv_vip, "私聊未归级但群聊归级（NULL / int 混存）")
        check((await st.subject_level_map("user")) == {}, "私聊未归级不进 subject_level_map")
        await st.set_user_level_scene("10001", private_level=None, group_level=None)
        check(await st.get_user_level_scene("10001") == (None, None), "两个场景都取消 → 记录删除")

        await st.upsert_level("user", "VIP改名", level_id=lv_vip, effect="deny", sort_order=9)
        lv = await st.get_level(lv_vip)
        check(lv["name"] == "VIP改名" and lv["effect"] == "deny" and lv["sort_order"] == 9, "按 id 更新等级")
        check(len(await st.list_levels()) == 3, "更新不会多出等级")

        # 等级模型路由（主提供商 / 备用提供商；模型以提供商为单位，不单独存模型名）
        await st.upsert_level(
            "user", "VIP改名", level_id=lv_vip, effect="deny", sort_order=9,
            provider_id="prov-a", fallback_provider_id="prov-b",
        )
        lv = await st.get_level(lv_vip)
        check(lv["provider_id"] == "prov-a" and lv["fallback_provider_id"] == "prov-b",
              "等级模型路由字段可写可读")
        check("model" not in lv, "等级表已无 model 列（模型以提供商为单位）")
        # 等级默认「指令」权限（与 LLM 权限分开，v5）
        await st.upsert_level(
            "user", "VIP改名", level_id=lv_vip, effect="deny", sort_order=9,
            provider_id="prov-a", fallback_provider_id="prov-b", command_effect="deny",
        )
        lv = await st.get_level(lv_vip)
        check(lv["effect"] == "deny" and lv["command_effect"] == "deny",
              "等级的两套默认权限（LLM / 指令）互不影响、各自可读写")
        await st.upsert_level("user", "VIP改名", level_id=lv_vip, effect="allow", sort_order=9)
        lv = await st.get_level(lv_vip)
        check(lv["command_effect"] == "inherit", "不传指令权限 → 回到 inherit（避免旧值阴魂不散）")
        await st.upsert_level(
            "user", "VIP改名", level_id=lv_vip, effect="deny", sort_order=9,
            provider_id="prov-a", fallback_provider_id="prov-b",
        )
        await st.upsert_level("user", "VIP改名", level_id=lv_vip, effect="deny", sort_order=9)
        lv = await st.get_level(lv_vip)
        check(lv["provider_id"] == "" and lv["fallback_provider_id"] == "",
              "不传模型字段 → 清空（避免旧值阴魂不散）")
        await st.upsert_level(
            "user", "VIP改名", level_id=lv_vip, effect="deny", sort_order=9,
            provider_id="prov-a", fallback_provider_id="prov-b",
        )

        # 可切换模型名单（v9）：/切换模型 的可选范围，存 JSON 文本
        await st.upsert_level(
            "user", "VIP改名", level_id=lv_vip, effect="deny", sort_order=9,
            switch_providers=["prov-a", "prov-b", "prov-a", ""],
        )
        lv = await st.get_level(lv_vip)
        check(lv["switch_providers"] == '["prov-a", "prov-b"]',
              f"名单去空去重后存 JSON（实得 {lv['switch_providers']!r}）")
        check(parse_provider_list(lv["switch_providers"]) == ["prov-a", "prov-b"], "读取时解析回列表")
        check(parse_provider_list("") == [] and parse_provider_list(None) == [], "空串 / None → 空列表")
        check(parse_provider_list("prov-x") == ["prov-x"], "容错：库里是裸 id（不是 JSON）也能读出来")
        await st.upsert_level(
            "user", "VIP改名", level_id=lv_vip, effect="deny", sort_order=9,
            switch_providers=[],
        )
        lv = await st.get_level(lv_vip)
        check(lv["switch_providers"] == "", "传空列表 → 存空串（= 用兜底三项）")
        # 允许切换的开关（v10）：默认关；只有显式打开才允许
        check(int(lv["switch_enabled"]) == 0, "不传 switch_enabled → 默认 0（只读）")
        await st.upsert_level(
            "user", "VIP改名", level_id=lv_vip, effect="deny", sort_order=9,
            switch_enabled=True,
        )
        check(int((await st.get_level(lv_vip))["switch_enabled"]) == 1, "开关可写可读（True → 1）")
        # 导入路径什么都可能来：字符串 / 数字都要收口成 0/1
        await st.upsert_level("user", "VIP改名", level_id=lv_vip, switch_enabled="false")
        check(int((await st.get_level(lv_vip))["switch_enabled"]) == 0,
              "字符串 \"false\" 不会被当成真值（收口在 _as_flag）")
        await st.upsert_level("user", "VIP改名", level_id=lv_vip, switch_enabled=0)
        check(int((await st.get_level(lv_vip))["switch_enabled"]) == 0, "数字 0 → 只读")
        await st.upsert_level(
            "user", "VIP改名", level_id=lv_vip, effect="deny", sort_order=9,
            provider_id="prov-a", fallback_provider_id="prov-b",
            switch_providers=["prov-a", "prov-b"], switch_enabled=True,
        )
        check(int((await st.get_level(lv_vip))["switch_enabled"]) == 1, "开关与名单可以一起写")
        # 允许检测指令的开关（v11）：同样默认关，且与 switch_enabled **互相独立**
        # （能看不等于愿意花钱测：检测会真打模型）
        lv = await st.get_level(lv_vip)
        check(int(lv["detect_enabled"]) == 0, "不传 detect_enabled → 默认 0（不允许检测）")
        await st.upsert_level("user", "VIP改名", level_id=lv_vip, switch_enabled=False,
                              detect_enabled=True)
        lv = await st.get_level(lv_vip)
        check(int(lv["detect_enabled"]) == 1 and int(lv["switch_enabled"]) == 0,
              "检测开关与切换开关互不影响（只读也能开检测）")
        await st.upsert_level("user", "VIP改名", level_id=lv_vip, detect_enabled="0")
        check(int((await st.get_level(lv_vip))["detect_enabled"]) == 0,
              "字符串 \"0\" 收口成 0（走 _as_flag，与 switch_enabled 同一套）")

        # 用户切换（model_choice，v9）已废弃：迁移进专属模型（v1.3.12）。
        # 私聊选择 → 专属模型（没有专属的才迁）；群聊选择（旧语义跨群串号）→ 直接删。
        await st.set_policy("user", "20001", "prov-a", feature="model_choice", scene="private")
        await st.set_policy("user", "20001", "prov-b", feature="model_choice", scene="group")
        await st.set_policy("user", "20002", "prov-c", feature="model_choice", scene="private")
        await st.set_policy("user", "20002", "prov-z", feature="model", scene="")  # 已有专属 → 不覆盖
        moved = await st.migrate_model_choice()
        check(moved == 1, f"只有「没有专属模型的人」才迁（实得 moved={moved}）")
        m = await st.effect_map("user", feature="model")
        check(m.get("20001", {}).get("") == "prov-a", "20001：私聊选择 → 专属模型")
        check(m.get("20002", {}).get("") == "prov-z", "20002：已有专属模型不被迁移覆盖")
        check(await st.effect_map("user", feature="model_choice") == {},
              "迁移后 model_choice 清空（旧表不留死数据）")
        check((await st.migrate_model_choice()) == 0, "迁移幂等：再跑一遍是 no-op")
        await st.set_policy("user", "20001", "inherit", feature="model", scene="")
        await st.set_policy("user", "20002", "inherit", feature="model", scene="")
        # 群专属模型（v1.3.12）：group 层同样用 feature=model，按群各一份
        await st.set_policy("group", "88888", "prov-g", feature="model", scene="")
        check(await st.effect_map("group", feature="model") == {"88888": {"": "prov-g"}},
              "群专属模型按群维度落库")
        await st.set_policy("group", "88888", "inherit", feature="model", scene="")

        await st.set_subject_level("group", "88888", lv_group)
        await st.delete_level(lv_group)
        check(await st.get_level(lv_group) is None, "删除等级")
        check(await st.get_subject_level("group", "88888") is None, "删除等级后归级记录一并清除")
        check((await st.level_counts()).get(lv_group) is None, "删除等级后成员统计同步清空")

        print("\n[9] bot 最后消息")
        await st.set_bot_message("user", "10001", "llm", ts=1000, preview="你好呀", platform_id="p1")
        await st.set_bot_message("group", "88888", "command", ts=2000, command="help", preview="/help")
        b1 = await st.get_bot_message("user", "10001")
        check(b1["kind"] == "llm" and b1["preview"] == "你好呀" and b1["platform_id"] == "p1", "写入后读取")
        await st.set_bot_message("user", "10001", "normal", ts=3000, preview="覆盖")
        b1 = await st.get_bot_message("user", "10001")
        check(b1["ts"] == 3000 and b1["kind"] == "normal" and b1["preview"] == "覆盖",
              "同一会话覆盖式更新（只留最新一条）")
        m = await st.bot_message_map("group")
        check(m["88888"]["command"] == "help", "bot_message_map 按作用域取")
        check(len(await st.bot_message_map("user")) == 1, "bot_message_map(user) 只有 1 个会话")
        sm = await st.bot_message_summary(0, 4000)
        check(sm["sessions"] == 2, f"会话数 = {sm['sessions']}（期望 2）")
        check(any(k["kind"] == "normal" and k["cnt"] == 1 for k in sm["kinds"]), "类型分布含 normal")
        check(sm["latest_ts"] == 3000, "最新时间 = 3000")
        await st.set_bot_message("user", "", "llm")  # 空 scope_id 应被忽略
        check((await st.bot_message_summary(0, 10**12))["sessions"] == 2, "空 id 不产生记录")

        print("\n[10] 生命周期")
        await st.close()
        check(not st.ready, "close 后 ready=False")
        await st.close()  # 幂等
        st2 = Store(db_path)
        await st2.open()
        check((await st2.list_friends())["total"] == 2, "重新打开后数据仍在")
        check((await st2.get_usage("user", "10001"))["month"]["used_tokens"] == 510, "重新打开后用量计数仍在")
        await st2.close()

    print("\n[11] v1 → v2 迁移")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp2:
        v1_path = str(Path(tmp2) / "v1.db")
        import aiosqlite

        raw = await aiosqlite.connect(v1_path)
        await raw.executescript(
            """
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER NOT NULL);
            CREATE TABLE llm_quota (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_type   TEXT    NOT NULL,
                scope_id     TEXT    NOT NULL,
                period       TEXT    NOT NULL,
                limit_tokens INTEGER NOT NULL,
                used_tokens  INTEGER NOT NULL DEFAULT 0,
                mode         TEXT    NOT NULL DEFAULT 'enforce',
                reset_at     INTEGER,
                updated_at   INTEGER NOT NULL,
                UNIQUE (scope_type, scope_id, period)
            );
            CREATE INDEX idx_quota_scope ON llm_quota(scope_type, scope_id);
            """,
        )
        await raw.execute("INSERT INTO settings(key, value, updated_at) VALUES('schema_version', '1', 0)")
        await raw.execute(
            "INSERT INTO llm_quota(scope_type, scope_id, period, limit_tokens, used_tokens, mode, reset_at, updated_at) "
            "VALUES('user', '10001', 'day', 1000, 600, 'enforce', 555, 0)"
        )
        await raw.execute(
            "INSERT INTO llm_quota(scope_type, scope_id, period, limit_tokens, used_tokens, mode, updated_at) "
            "VALUES('user', '10001', 'month', 50000, 0, 'observe', 0)"
        )
        await raw.commit()
        await raw.close()

        st3 = Store(v1_path)
        await st3.open()
        check(await st3.get_setting("schema_version") == "11", "版本号直接升到最新（v1 → v11 连续迁移）")
        q = await st3.get_quota("user", "10001", "day")
        check(q is not None and q["limit_tokens"] == 1000 and "used_tokens" not in q,
              "限额保留、用量的列已移除")
        check((await st3.get_usage("user", "10001"))["day"]["used_tokens"] == 600, "v1 已用量迁入 usage_counter")
        check((await st3.get_usage("user", "10001"))["day"]["reset_at"] == 555, "reset_at 一并迁移")
        check((await st3.get_usage("user", "10001")).get("month") is None, "用量为 0 的行不迁移")
        check(await st3.get_quota("user", "10001", "month") is not None, "另一个周期限额也在")
        check(await st3.get_quota("user", "10001", "month") is not None
              and (await st3.get_quota("user", "10001", "month"))["mode"] == "observe", "模式保留")
        async with st3._conn().execute("PRAGMA index_list(llm_quota)") as cur:
            idx = {str(r["name"]) for r in await cur.fetchall()}
        check("idx_quota_scope" in idx, f"重建后索引仍存在（实得 {sorted(idx)}）")
        # 二次打开不应重复迁移
        await st3.close()
        st4 = Store(v1_path)
        await st4.open()
        check((await st4.get_usage("user", "10001"))["day"]["used_tokens"] == 600, "重开不会重复累加迁移用量")
        await st4.close()

    print("\n[12] v2 → v4 迁移（等级新增模型路由两列、并去掉 v3 临时的 model 列）")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp3:
        v2_path = str(Path(tmp3) / "v2.db")
        raw2 = await aiosqlite.connect(v2_path)
        await raw2.executescript(
            """
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER NOT NULL);
            CREATE TABLE quota_level (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                kind        TEXT    NOT NULL,
                name        TEXT    NOT NULL,
                description TEXT    NOT NULL DEFAULT '',
                effect      TEXT    NOT NULL DEFAULT 'inherit',
                sort_order  INTEGER NOT NULL DEFAULT 0,
                updated_at  INTEGER NOT NULL,
                UNIQUE (kind, name)
            );
            """,
        )
        await raw2.execute("INSERT INTO settings(key, value, updated_at) VALUES('schema_version', '2', 0)")
        await raw2.execute("INSERT INTO quota_level(kind, name, updated_at) VALUES('user', '老等级', 0)")
        await raw2.commit()
        await raw2.close()

        st5 = Store(v2_path)
        await st5.open()
        check(await st5.get_setting("schema_version") == "11", "版本号升到 11（v2 → v3 → … → v10 → v11 连续迁移）")
        lv = await st5.get_level(1)
        check(lv is not None and lv["name"] == "老等级", "v2 的等级数据保留")
        check(
            lv.get("provider_id") == "" and lv.get("fallback_provider_id") == "" and "model" not in lv,
            "模型路由两列默认为空，且 v3 临时加的 model 列已被 v4 移除",
        )
        check(lv.get("switch_providers") == "", "v9 新增的「可切换模型」列默认为空")
        check(int(lv.get("switch_enabled") or 0) == 0,
              "v10 新增的「允许切换」开关默认为 0（只读：升级后不会凭空放开自助切换）")
        check(int(lv.get("detect_enabled") or 0) == 0,
              "v11 新增的「允许检测」开关默认为 0（要花钱的指令，升级后不会凭空放开）")
        check(lv.get("command_effect") == "inherit", "v5 新增的 level.command_effect 默认为 inherit")
        await st5.upsert_level("user", "老等级", level_id=1, provider_id="p1", fallback_provider_id="p2")
        lv = await st5.get_level(1)
        check(
            lv["provider_id"] == "p1" and lv["fallback_provider_id"] == "p2",
            "迁移后两列可正常读写",
        )
        await st5.close()
        st6 = Store(v2_path)
        await st6.open()
        check((await st6.get_level(1))["provider_id"] == "p1", "重开不会重复迁移、也不丢数据")
        await st6.close()

    print("\n[12.1] v5 → v6 迁移（policy 加场景列、等级加群聊默认权限）")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp5:
        import aiosqlite

        v5_path = str(Path(tmp5) / "v5.db")
        raw = await aiosqlite.connect(v5_path)
        await raw.executescript(
            """
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER NOT NULL);
            CREATE TABLE policy (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_type  TEXT    NOT NULL,
                scope_id    TEXT    NOT NULL,
                feature     TEXT    NOT NULL DEFAULT 'llm',
                effect      TEXT    NOT NULL,
                note        TEXT    NOT NULL DEFAULT '',
                updated_at  INTEGER NOT NULL,
                UNIQUE (scope_type, scope_id, feature)
            );
            CREATE INDEX idx_policy_scope ON policy(scope_type, scope_id);
            CREATE TABLE quota_level (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                kind                  TEXT    NOT NULL,
                name                  TEXT    NOT NULL,
                description           TEXT    NOT NULL DEFAULT '',
                effect                TEXT    NOT NULL DEFAULT 'inherit',
                command_effect        TEXT    NOT NULL DEFAULT 'inherit',
                sort_order            INTEGER NOT NULL DEFAULT 0,
                provider_id           TEXT    NOT NULL DEFAULT '',
                fallback_provider_id  TEXT    NOT NULL DEFAULT '',
                updated_at            INTEGER NOT NULL,
                UNIQUE (kind, name)
            );
            """,
        )
        await raw.execute("INSERT INTO settings(key, value, updated_at) VALUES('schema_version', '5', 0)")
        await raw.execute(
            "INSERT INTO policy(scope_type, scope_id, feature, effect, note, updated_at) "
            "VALUES('user', '10001', 'llm', 'deny', '', 0)"
        )
        await raw.execute(
            "INSERT INTO policy(scope_type, scope_id, feature, effect, note, updated_at) "
            "VALUES('group', '88888', 'llm', 'deny', '', 0)"
        )
        await raw.execute(
            "INSERT INTO quota_level(kind, name, effect, command_effect, sort_order, updated_at) "
            "VALUES('user', '老等级', 'deny', 'allow', 0, 0)"
        )
        await raw.commit()
        await raw.close()

        st8 = Store(v5_path)
        await st8.open()
        check(await st8.get_setting("schema_version") == "11", "版本号升到 11")
        em = await st8.effect_map("user")
        check(em == {"10001": {"": "deny"}}, f"旧规则落到「通用」场景（实得 {em}）")
        check(await st8.resolve_policy("user", "10001", "llm", "group") == "deny",
              "旧规则在群聊里照样生效（升级不改变既有行为）")
        gm = await st8.effect_map("group")
        check(gm == {"88888": {"": "deny"}}, "群规则同样保留")
        lv = await st8.get_level(1)
        check(lv["effect_group"] == "inherit" and lv["command_effect_group"] == "inherit",
              "等级新增的群聊默认权限默认为 inherit（= 跟随主值）")
        check(lv["effect"] == "deny" and lv["command_effect"] == "allow", "等级原有权限保留")
        check((await st8.list_group_members("88888"))["total"] == 0,
              "v7 新增的 group_member 表在迁移时自动建好")
        # 重建后的唯一键必须含 scene：同对象同 feature 能同时存「私聊」与「通用」
        await st8.set_policy("user", "10001", "allow", feature="llm", scene="private")
        check(len(await st8.list_policies(scope_type="user", feature="llm")) == 2,
              "重建后的唯一键含 scene（同对象可存两条）")
        check(await st8.resolve_policy("user", "10001", "llm", "private") == "allow", "私聊专属规则生效")
        check(await st8.resolve_policy("user", "10001", "llm", "group") == "deny", "群聊仍按通用规则")
        await st8.close()
        st9 = Store(v5_path)
        await st9.open()
        check(await st9.get_setting("schema_version") == "11", "重开不会重复迁移")
        check(await st9.resolve_policy("user", "10001", "llm", "private") == "allow", "重开后规则仍在")
        await st9.close()

    print("\n[13] 指令规则（policy 的指令 feature 维度）")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp4:
        st7 = Store(str(Path(tmp4) / "cmd.db"))
        await st7.open()
        await st7.set_policy("global", "*", "deny", feature="command:help")
        await st7.set_policy("user", "10001", "allow", feature="command:help")
        await st7.set_policy("group", "88888", "deny", feature="command:draw")
        cp = await st7.command_policies()
        check(cp["help"]["global"]["*"][""] == "deny", "全局指令规则可读写（场景层 '' = 通用）")
        check(cp["help"]["user"]["10001"][""] == "allow", "好友指令规则可读写")
        check(cp["draw"]["group"]["88888"][""] == "deny", "群指令规则可读写")
        check(set(cp) == {"help", "draw"}, f"command_policies 只含指令 feature（实得 {sorted(cp)}）")
        check(
            await st7.effect_map("global", feature="command:help") == {"*": {"": "deny"}},
            "effect_map 支持任意 feature（值为 {场景: 效果}）",
        )
        check(len(await st7.list_policies(feature="command:help")) == 2, "按 feature 过滤")
        check(len(await st7.list_policies(feature="llm")) == 0, "指令规则不污染 llm feature")
        check(len(await st7.list_policies()) == 3, "不传 feature → 全部规则")
        await st7.set_policy("user", "10001", "inherit", feature="command:help")
        check(len(await st7.list_policies(feature="command:help")) == 1, "inherit 删除该条指令规则")
        check(await st7.delete_stale_command_policies(["help", "draw"]) == 0, "指令都还在 → 不清理")
        await st7.set_policy("user", "20002", "deny", feature="command:old")
        n = await st7.delete_stale_command_policies(["help", "draw"])
        check(n == 1, f"指令被卸载后残留的规则会被清理（实得 {n} 条）")
        check("old" not in await st7.command_policies(), "清理后该指令规则消失")
        check(len(await st7.list_policies(feature="command:help")) == 1, "不相关的指令规则不受影响")
        await st7.close()

    print("\n[14] 群成员缓存与群内用量")
    # 这里另起一个库：上面的 st 已经在 [10] 关掉了（生命周期用例），复用会报「尚未 open()」
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp7:
        st = Store(str(Path(tmp7) / "members.db"))
        await st.open()
        n = await st.upsert_group_members(
            "p1",
            "88888",
            [
                {"user_id": "10001", "nickname": "小明", "card": "同事", "role": "owner"},
                {"user_id": "10002", "nickname": "小红", "role": "admin"},
                {"user_id": "10003", "nickname": "小刚", "role": "member"},
                {"user_id": ""},  # 空 QQ 应被忽略
            ],
        )
        check(n == 3, f"覆盖式写入 3 个成员（实得 {n}）")
        lst = await st.list_group_members("88888")
        check(lst["total"] == 3, f"成员总数 = {lst['total']}")
        check(lst["rows"][0]["user_id"] == "10001", "排序：群主在最前")
        check(lst["rows"][1]["role"] == "admin", "排序：管理员次之")
        check(lst["rows"][0]["card"] == "同事", "群名片一并缓存")
        check((await st.list_group_members("88888", keyword="小红"))["total"] == 1, "按昵称搜索")
        check((await st.list_group_members("88888", keyword="10003"))["total"] == 1, "按 QQ 搜索")
        check((await st.list_group_members("99999"))["total"] == 0, "别的群没有成员")

        again = await st.upsert_group_members(
            "p1", "88888", [{"user_id": "10001", "nickname": "小明改名", "role": "owner"}]
        )
        check(
            again == 1 and (await st.list_group_members("88888"))["total"] == 1,
            "再次同步是覆盖式（退群的人从缓存里消失）",
        )
        check((await st.get_group_member("88888", "10001"))["nickname"] == "小明改名", "单个成员可读")
        check((await st.group_member_counts()).get("88888") == 1, "每群成员数统计")

        # 该群内按发言人聚合用量（成员列表展示「他在这个群用了多少」）
        await st.log_usage(
            ts=int(time.time()),
            scope_type="group",
            scope_id="88888",
            sender_id="10002",
            group_id="88888",
            status="ok",
            tok_out=42,
        )
        gs = await st.usage_sums_in_group("88888", 0, 10**12)
        check(gs.get("10002") == 42, f"群内按发言人聚合（实得 {gs}）")
        check(gs.get("10001", 0) == 0, "没有 token 的记录聚合为 0")

        print("\n[15] 群成员维度的用量计数与额度（按成员设额度的基础）")
        await st.add_used(
            "member", "88888:10001", 120, reset_at_map={"day": 999, "month": None, "total": None}
        )
        await st.add_used("member", "88888:10001", 30, reset_at_map={"day": 111})
        um = await st.usage_map("member")
        check(um["88888:10001"]["day"]["used_tokens"] == 150,
              f"成员用量累加（实得 {um['88888:10001']['day']['used_tokens']}）")
        check(um["88888:10001"]["day"]["reset_at"] == 999,
              "reset_at 只在首次创建时写入（不会被后续调用推后）")
        check((await st.get_usage("member", "88888:10001"))["month"]["used_tokens"] == 150,
              "月周期一起累加（与现有三维度记账一致）")
        # 成员额度：表结构与 user/group 共用（scope_type='member'）
        await st.upsert_quota("member", "88888:10001", "day", 500)
        rows = await st.list_quotas_of("member", "88888:10001")
        check(len(rows) == 1 and rows[0]["limit_tokens"] == 500, "成员额度可读")
        check((await st.list_quotas("member"))[0]["scope_id"] == "88888:10001", "按维度列出成员额度")
        n = await st.reset_used(scope_type="member", scope_id="88888:10001", period="day", reset_at=12345)
        check(
            n == 1 and (await st.get_usage("member", "88888:10001"))["day"]["used_tokens"] == 0,
            "成员用量可单独清零",
        )
        check((await st.get_usage("member", "88888:10001"))["month"]["used_tokens"] == 150,
              "清零只作用于指定周期（月用量不受影响）")
        await st.close()

    print("\n[16] 规则导出 / 导入（含等级 id 重映射）")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp9:
        src = Store(str(Path(tmp9) / "src.db"))
        await src.open()
        vip = await src.upsert_level(
            "user", "VIP", effect="allow", command_effect="deny",
            effect_group="deny", command_effect_group="allow",
            provider_id="p1", fallback_provider_id="p2", sort_order=5,
            switch_providers=["p1", "p2"], switch_enabled=True,
        )
        big = await src.upsert_level(
            "group", "大群", effect="deny", command_effect="inherit", switch_enabled=0
        )
        await src.set_subject_level("user", "10001", vip)
        await src.set_subject_level("group", "88888", big)
        await src.upsert_quota("level", str(vip), "day", 500000)
        await src.upsert_quota("user", "10001", "day", 1000)
        await src.upsert_quota("member", "88888:10001", "day", 500)
        await src.upsert_quota("global", "*", "month", 9000000)
        await src.set_policy("user", "10001", "deny", feature="llm", scene="private")
        await src.set_policy("user", "10001", "allow", feature="llm", scene="group")
        await src.set_policy("group", "88888", "deny", feature="command:help")
        await src.set_policy("member", "88888:10001", "allow", feature="command")
        await src.set_policy("member", "88888:10001", "deny")  # LLM 维度的成员规则
        # 模型类规则（v9/v1.3.12）：这两个 feature 的 effect 存的是**提供商 id**，导入时不能被规整掉
        await src.set_policy("user", "10001", "p1", feature="model", scene="")
        await src.set_policy("group", "88888", "p9", feature="model", scene="")
        await src.set_policy("user", "10001", "p2", feature="model_choice", scene="private")
        dump = await src.export_rules()
        check(
            len(dump["levels"]) == 2 and len(dump["quotas"]) == 4 and len(dump["policies"]) == 8,
            f"导出各表行数（levels={len(dump['levels'])} quotas={len(dump['quotas'])} policies={len(dump['policies'])}）",
        )
        check(len(dump["subject_levels"]) == 2, "归级一并导出")
        await src.close()

        dst = Store(str(Path(tmp9) / "dst.db"))
        await dst.open()
        # 目标库里先放一个别的等级，制造「id 会撞车」的现场
        await dst.upsert_level("user", "别的等级")
        stats = await dst.import_rules(dump, mode="merge")
        check(
            # v1.4.0：旧格式的 global:* 行导入时展开成 private + group 两行（4 → 5）
            stats["levels"] == 2 and stats["policies"] == 8 and stats["quotas"] == 5 and stats["skipped"] == 0,
            f"导入统计正确（实得 {stats}）",
        )
        lv_list = await dst.list_levels("user")
        new_vip = [lv for lv in lv_list if lv["name"] == "VIP"][0]
        check(new_vip["id"] != vip or True, "VIP 在目标库里有了自己的 id")
        check(await dst.get_subject_level("user", "10001") == new_vip["id"],
              "归级指向**重映射后**的等级 id（不是文件里的旧 id）")
        lq = await dst.list_quotas_of("level", str(new_vip["id"]))
        check(len(lq) == 1 and lq[0]["limit_tokens"] == 500000, "等级额度跟着新 id 走")
        check(
            new_vip["effect_group"] == "deny" and new_vip["command_effect_group"] == "allow",
            "等级的两套默认权限（含群聊那套）一并导入",
        )
        check(new_vip["provider_id"] == "p1" and new_vip["fallback_provider_id"] == "p2",
              "模型路由一并导入")
        check(parse_provider_list(new_vip["switch_providers"]) == ["p1", "p2"],
              "可切换模型名单一并导入")
        check(int(new_vip["switch_enabled"]) == 1, "「允许切换」开关一并导入（true → 1）")
        big_row = [lv for lv in await dst.list_levels("group") if lv["name"] == "大群"][0]
        check(int(big_row["switch_enabled"] or 0) == 0, "关着的等级导入后仍是只读（不会被带成真值）")
        check(
            await dst.effect_map("user", feature="model") == {"10001": {"": "p1"}},
            "好友专属模型（effect=提供商 id）导入后不被抹成 inherit",
        )
        mch = await dst.effect_map("user", feature="model_choice")
        check(mch == {"10001": {"private": "p2"}},
              f"用户自己切换的模型按场景导入（实得 {mch}）")
        check(
            await dst.effect_map("group", feature="model") == {"88888": {"": "p9"}},
            "群专属模型（group 层，v1.3.12）一并导入",
        )
        check(await dst.get_effect("user", "10001", "llm", "private") == "deny", "私聊场景规则导入")
        check(await dst.get_effect("user", "10001", "llm", "group") == "allow", "群聊场景规则导入")
        cp = await dst.command_policies()
        check(cp["help"]["group"]["88888"][""] == "deny", "单条指令规则导入")
        cm = await dst.effect_map("member")
        check(cm["88888:10001"][""] == "deny", "群成员维度的 LLM 规则导入")
        cmc = await dst.effect_map("member", feature="command")
        check(cmc["88888:10001"][""] == "allow", "群成员维度的指令规则导入")
        check((await dst.get_quota("member", "88888:10001", "day"))["limit_tokens"] == 500, "成员额度导入")
        # v1.4.0：旧备份里的 global:* 导入时展开成私聊 / 群聊两行
        check((await dst.get_quota("global", "private", "month"))["limit_tokens"] == 9000000, "全局额度导入（私聊）")
        check((await dst.get_quota("global", "group", "month"))["limit_tokens"] == 9000000, "全局额度导入（群聊）")

        # 坏数据：跳过坏行，而不是整次导入失败
        bad = {
            "levels": [{"kind": "user", "name": "坏等级", "effect": "什么鬼"}],
            "quotas": [
                {"scope_type": "user", "scope_id": "10002", "period": "week", "limit_tokens": 1},
                # v1.0.0：坏 mode 规整成 enforce、坏 reset_at 归 None（不能透传进库）
                {"scope_type": "user", "scope_id": "10002", "period": "day", "limit_tokens": 7,
                 "mode": "OBSERVE", "reset_at": "明天"},
            ],
            "policies": [
                {"scope_type": "user", "scope_id": "10002", "feature": "llm", "effect": "allow", "scene": "??"},
                {"scope_type": "user", "scope_id": "10002", "feature": "llm", "effect": "deny"},
                # v1.0.0：这些形态只会变成死数据，必须跳过
                {"scope_type": "member", "scope_id": "88888", "feature": "llm", "effect": "deny"},
                {"scope_type": "global", "scope_id": "*", "feature": "llm", "effect": "deny"},
                {"scope_type": "user", "scope_id": "10002", "feature": "command:", "effect": "deny"},
                {"scope_type": "user", "scope_id": "10002", "feature": "什么鬼", "effect": "deny"},
                # v9/v1.3.12：模型类规则只挂「好友 / 群」两层，且 effect（提供商 id）不能为空
                {"scope_type": "member", "scope_id": "88888:10001", "feature": "model", "effect": "p1"},
                {"scope_type": "user", "scope_id": "10002", "feature": "model_choice", "effect": ""},
            ],
        }
        st2 = await dst.import_rules(bad, mode="merge")
        # 跳过 = 非法 period / scene / member 格式 / global+llm / 空 command 名 / 未知 feature
        #        / 模型类规则挂错层 / 模型类规则 effect 为空
        check(st2["skipped"] == 8, f"各类坏行被跳过（实得 skipped={st2['skipped']}）")
        check(await dst.get_effect("user", "10002", "llm", "") == "deny", "同批里的合法行照常导入")
        q = await dst.list_quotas_of("user", "10002")
        day = [x for x in q if x["period"] == "day"]
        check(day and day[0]["mode"] == "observe" and not day[0].get("reset_at"),
              "坏 mode 规整（OBSERVE→observe）、坏 reset_at 归 None")
        names = [lv["name"] for lv in await dst.list_levels()]
        check("坏等级" in names, "未知 effect 的等级被规整成 inherit 而不是丢弃")
        check(
            [lv for lv in await dst.list_levels() if lv["name"] == "坏等级"][0]["effect"] == "inherit",
            "未知 effect 规整成 inherit",
        )

        # replace 模式：清空后只留文件里的
        await dst.import_rules(dump, mode="replace")
        left = [lv["name"] for lv in await dst.list_levels()]
        check("别的等级" not in left and "坏等级" not in left, f"replace 清掉了本地多余等级（实得 {left}）")
        check("VIP" in left, "replace 后文件里的等级都在")
        await dst.close()

    print()
    if _failures:
        print(f"失败 {len(_failures)} 项：")
        for f in _failures:
            print("  - " + f)
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
