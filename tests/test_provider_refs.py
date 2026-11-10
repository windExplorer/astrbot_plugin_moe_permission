"""AstrBot 删除 / 禁用模型后的悬空引用：分类、校验、定向清理自检。

用法：
    uv run --no-project --with aiosqlite python tests/test_provider_refs.py

为什么要单独测：这里全是不可逆的写操作（清掉用户配好的等级路由）。
核心红线是**绝不自动清理**：禁用可逆、加载有时序，只有用户主动勾选、
且服务端二次确认「当前仍未加载」时才动手。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import provider_refs as PR  # noqa: E402
from store import Store  # noqa: E402

_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ok   " if cond else "  MISS ") + label)
    if not cond:
        _failures.append(label)


def levels():
    return [
        {"id": 1, "kind": "user", "name": "VIP", "provider_id": "p-alive",
         "fallback_provider_id": "p-deleted", "switch_providers": '["p-alive","p-disabled"]'},
        {"id": 2, "kind": "group", "name": "普通群", "provider_id": "p-unavailable",
         "fallback_provider_id": "", "switch_providers": ""},
        # 老库里手填的裸 id（JSON 解不开）也必须被发现
        {"id": 3, "kind": "user", "name": "旧等级", "provider_id": "",
         "fallback_provider_id": "", "switch_providers": "p-deleted"},
    ]


def policies():
    return [
        {"scope_type": "user", "scope_id": "10001", "feature": "model",
         "scene": "", "effect": "p-deleted"},
        {"scope_type": "group", "scope_id": "888", "feature": "model",
         "scene": "group", "effect": "p-alive"},
        # 非模型 feature 不能被当成 provider id
        {"scope_type": "user", "scope_id": "10002", "feature": "llm",
         "scene": "", "effect": "deny"},
        {"scope_type": "user", "scope_id": "10003", "feature": "model",
         "scene": "", "effect": "inherit"},
    ]


def configured():
    return {
        "p-alive": {"id": "p-alive", "enable": True},
        "p-disabled": {"id": "p-disabled", "enable": False},
        "p-unavailable": {"id": "p-unavailable", "enable": True},
    }


def test_collect() -> None:
    print("[引用收集]")
    refs = PR.collect_refs(levels(), policies())
    check(set(refs) == {"p-alive", "p-deleted", "p-disabled", "p-unavailable"},
          f"四个 id 全部收集：{sorted(refs)}")
    check(len(refs["p-deleted"]) == 3, "被 3 处引用（备用 / 裸名单 / 专属模型）")
    check("deny" not in refs and "inherit" not in refs,
          "llm 规则的 allow/deny、model 的 inherit 不被当成模型 id")
    check(any(r["field"] == "switch_providers" for r in refs["p-disabled"]),
          "JSON 名单里的引用带上了字段名")
    check(any(r["where"] == "policy" and r["scope_id"] == "10001" for r in refs["p-deleted"]),
          "专属模型引用带上了对象 id")
    check(PR.parse_switch_providers("p-deleted") == ["p-deleted"], "裸 id 兜底与 store 同语义")
    check(PR.parse_switch_providers('["a","a","b"]') == ["a", "b"], "去空去重保序")
    check(PR.parse_switch_providers("") == [], "空串 = 空名单")


def test_classify() -> None:
    print("[状态分类]")
    alive = ["p-alive", "p-deleted-reused"]
    cfg = configured()
    check(PR.classify("p-alive", alive, cfg) == PR.STATUS_AVAILABLE, "已加载 → available")
    check(PR.classify("p-disabled", alive, cfg) == PR.STATUS_DISABLED, "配置在、enable=false → disabled")
    check(PR.classify("p-unavailable", alive, cfg) == PR.STATUS_UNAVAILABLE,
          "配置在、enable=true 但没加载 → unavailable")
    check(PR.classify("p-gone", alive, cfg) == PR.STATUS_DELETED, "配置里没有 → deleted")
    check(PR.classify("p-gone", alive, None) == PR.STATUS_UNKNOWN,
          "读不到配置 → unknown（绝不推断成 deleted）")


def test_report() -> None:
    print("[报告]")
    report = PR.build_report(PR.collect_refs(levels(), policies()), ["p-alive"], configured())
    by_id = {it["provider_id"]: it for it in report["stale"]}
    check(set(by_id) == {"p-deleted", "p-disabled", "p-unavailable"}, "只报失效的，已加载的不报")
    check(by_id["p-deleted"]["status"] == "deleted", "已删除")
    check(by_id["p-disabled"]["status"] == "disabled", "已禁用")
    check(by_id["p-unavailable"]["status"] == "unavailable", "未加载")
    check(report["counts"]["deleted"] == 1 and report["total"] == 3, "计数正确")
    check(PR.stale_ids(report) == set(by_id), "stale_ids 与报告一致")

    unknown = PR.build_report(PR.collect_refs(levels(), policies()), ["p-alive"], None)
    check(all(it["status"] == PR.STATUS_UNKNOWN for it in unknown["stale"]),
          "读不到配置时全部标 unknown")
    check(unknown["configured_known"] is False, "报告显式标注配置不可读")

    clean = PR.build_report({"p-alive": [{"where": "level"}]}, ["p-alive"], configured())
    check(clean["total"] == 0 and clean["stale"] == [], "没有失效引用 → 空报告")

    print("[等级逐字段标记]")
    check(PR.stale_level_fields(levels()[0], PR.stale_ids(report)) == ["fallback_provider_id", "switch_providers"],
          "VIP 的备用与名单被标出")
    check(PR.stale_level_fields(levels()[2], PR.stale_ids(report)) == ["switch_providers"],
          "裸 id 名单也能标出")


def test_validate() -> None:
    print("[写入侧校验]")
    known = {"p-alive", "p-b"}
    old = {"provider_id": "p-disabled", "fallback_provider_id": "p-alive"}
    check(PR.validate_new_ids({"provider_id": "p-disabled", "fallback_provider_id": "p-alive"},
                              old, known) == [],
          "原值原样回传（模型被临时禁用）时放行，不阻塞编辑其它字段")
    check(PR.validate_new_ids({"provider_id": "p-gone"}, old, known) == ["p-gone"],
          "新填未加载的 id 被拦下")
    check(PR.validate_new_ids({"provider_id": "p-b"}, old, known) == [], "改成已加载的放行")
    check(PR.validate_new_ids({"provider_id": ""}, old, known) == [], "清空字段放行")
    check(PR.validate_new_ids({"provider_id": "p-gone"}, old, set()) == [],
          "拿不到 provider 列表时放行（不锁死控制台）")
    check(PR.filter_known(["p-alive", "p-gone"], known) == ["p-alive"], "名单过滤掉失效 id")
    check(PR.filter_known(["p-alive", "p-gone"], set()) == ["p-alive", "p-gone"],
          "拿不到列表时名单原样保留，不静默丢数据")


async def test_store() -> None:
    print("[真实建库：定向清理]")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        st = Store(str(Path(tmp) / "user_gateway.db"))
        await st.open()
        for lv in levels():
            await st.upsert_level(
                lv["kind"], lv["name"], provider_id=lv["provider_id"],
                fallback_provider_id=lv["fallback_provider_id"],
                switch_providers=lv["switch_providers"],
            )
        await st.set_policy("user", "10001", "p-deleted", feature="model")
        await st.set_policy("group", "888", "p-alive", feature="model")
        await st.set_policy("user", "10002", "deny", feature="llm")
        await st.log_usage(scope_type="user", scope_id="10001", provider_id="p-deleted",
                           model="old-model", tok_out=42)

        got_levels, got_policies = await st.model_ref_rows()
        refs = PR.collect_refs(got_levels, got_policies)
        check("p-deleted" in refs and "p-alive" in refs, "model_ref_rows 能喂给 collect_refs")

        # 定向清理：只清 p-deleted
        stats = await st.prune_provider_refs(["p-deleted"])
        check(stats["level_fields"] == 1, f"只清空命中 p-deleted 的备用模型字段：{stats}")
        check(stats["switch_removed"] == 1, f"名单里剔除命中 id：{stats}")
        check(stats["levels"] == 2, "只改了含 p-deleted 的两个等级")
        check(stats["policies"] == 1, "删掉一条专属模型 policy")

        after_levels, after_policies = await st.model_ref_rows()
        check(all(str(x.get("fallback_provider_id") or "") != "p-deleted" for x in after_levels),
              "等级备用模型不再有 p-deleted")
        check(all("p-deleted" not in PR.parse_switch_providers(x.get("switch_providers"))
                  for x in after_levels), "名单不再有 p-deleted")
        check(all(str(x.get("effect") or "") != "p-deleted" for x in after_policies),
              "专属模型不再有 p-deleted")
        check(any(str(x.get("effect") or "") == "p-alive" for x in after_policies),
              "未勾选的 p-alive 专属模型保留")
        check(any(str(x.get("effect") or "") == "deny" for x in after_policies),
              "非模型 feature 的 llm 规则不受影响")

        # 等级里的其它字段不能被误改
        vip = next(x for x in after_levels if x["name"] == "VIP")
        check(vip["provider_id"] == "p-alive" and vip["name"] == "VIP", "未命中的主模型与等级名保留")
        check("p-disabled" in PR.parse_switch_providers(vip["switch_providers"]),
              "名单里未勾选的 id 保序保留")

        # 历史用量不能被删
        usage = await st.query_usage(limit=20)
        rows = usage.get("rows") if isinstance(usage, dict) else usage
        check(any(str(r.get("provider_id") or "") == "p-deleted" for r in (rows or [])),
              "usage_log 里的历史 id 保留（对账用）")

        check(await st.prune_provider_refs([]) == {"levels": 0, "level_fields": 0,
                                                   "switch_removed": 0, "policies": 0},
              "空 ids 不做任何写操作")
        await st.close()


def main() -> None:
    test_collect()
    test_classify()
    test_report()
    test_validate()
    asyncio.run(test_store())


if __name__ == "__main__":
    main()
    print("\n失败 %d 项" % len(_failures))
    for f in _failures:
        print("  - " + f)
    sys.exit(1 if _failures else 0)
