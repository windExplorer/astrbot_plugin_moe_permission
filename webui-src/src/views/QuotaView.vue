<script setup lang="ts">
// 限额：三层结构 —— 全局默认额度 → 自定义等级额度（模板）→ 对象专属额度。
// 解析规则见下方「额度说明」卡片，与后端闸门（gate.py）的档位链严格一致。
import { computed, h, onMounted, ref } from "vue";
import {
  NAlert,
  NButton,
  NCard,
  NCheckbox,
  NCheckboxGroup,
  NDataTable,
  NEmpty,
  NForm,
  NFormItem,
  NGrid,
  NGridItem,
  NInput,
  NInputNumber,
  NModal,
  NProgress,
  NRadioButton,
  NRadioGroup,
  NSelect,
  NSpace,
  NSpin,
  NSwitch,
  NTabPane,
  NTabs,
  NTag,
  NTooltip,
  useMessage,
  type DataTableColumns,
} from "naive-ui";

import {
  apiDeleteLevel,
  apiGet,
  apiLevels,
  apiModelRefs,
  apiPost,
  apiProviders,
  apiPruneModelRefs,
  apiSetLevel,
  type LevelPayload,
  type LevelRow,
  type ModelRefsReport,
  type ProviderRow,
  type QuotaRow,
  type StaleModelRef,
} from "../api";

const message = useMessage();
const loading = ref(false);
const saving = ref(false);

// ------------------------------------------------------------------ //
// 失效模型引用（AstrBot 删除 / 禁用模型后，库里残留的旧 id）
// 只提示、由用户主动勾选清理；绝不自动删（禁用是可逆的、加载是有时序的）
// ------------------------------------------------------------------ //
const staleRefs = ref<StaleModelRef[]>([]);
const staleTotal = ref(0);
const configuredKnown = ref(true);
const showStaleModal = ref(false);
const staleLoading = ref(false);
const stalePruning = ref(false);
const staleReport = ref<ModelRefsReport | null>(null);
const staleChecked = ref<string[]>([]);
// 二次确认不用 useDialog：App.vue 只挂了 NMessageProvider，没有 NDialogProvider
const showPruneConfirm = ref(false);

const statusType: Record<string, "warning" | "error" | "default" | "info"> = {
  deleted: "error",
  disabled: "warning",
  unavailable: "info",
  unknown: "default",
};

async function openStaleModal() {
  showStaleModal.value = true;
  staleLoading.value = true;
  try {
    const report = await apiModelRefs();
    staleReport.value = report;
    // 用报告自己的口径：读不到 AstrBot 配置时禁止清理（会把「暂时没加载」误判成「已删除」）
    configuredKnown.value = report.configured_known !== false;
    // 默认只勾「已删除」的：禁用 / 未加载的随时可能回来，不替用户做主
    staleChecked.value = (report.stale || [])
      .filter((it) => it.status === "deleted")
      .map((it) => it.provider_id);
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    staleLoading.value = false;
  }
}

async function pruneStale() {
  if (!staleChecked.value.length) {
    message.warning("请先勾选要清理的模型");
    return;
  }
  showPruneConfirm.value = true;
}

async function doPrune() {
  stalePruning.value = true;
  try {
    const res = await apiPruneModelRefs(staleChecked.value);
    message.success(
      `已清理 ${res.pruned.length} 个模型${res.refused.length ? `（${res.refused.length} 个重新启用，已跳过）` : ""}`,
    );
    showPruneConfirm.value = false;
    showStaleModal.value = false;
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    stalePruning.value = false;
  }
}

const levels = ref<LevelRow[]>([]);
const globalRows = ref<QuotaRow[]>([]);
const specificRows = ref<QuotaRow[]>([]);
// AstrBot 里已加载的对话模型提供商（等级里「走哪个模型」只能从这里选：
// 不存在的提供商 id 会让 AstrBot 直接放弃本次请求）
const providers = ref<ProviderRow[]>([]);
const circuitOpen = ref<string[]>([]);
const defaultProviderId = ref("");

// 下拉选项：一项 = 一个「提供商 · 模型」（与 AstrBot / model_panel 的口径一致）
const providerOptions = computed(() =>
  providers.value.map((p) => {
    const marks = [
      p.is_default ? "当前默认" : "",
      circuitOpen.value.includes(p.id) ? "熔断中" : "",
    ].filter(Boolean);
    return {
      label: (p.label || p.model || p.id) + (marks.length ? `（${marks.join("、")}）` : ""),
      value: p.id,
    };
  }),
);

function providerLabel(id: string): string {
  if (!id) return "";
  const p = providers.value.find((x) => x.id === id);
  const base = p ? p.label || p.model || p.id : id;
  // 已删除 / 已禁用的模型不在 providers 里，别只显示一个裸 id 让用户一头雾水
  const dead = staleRefs.value.find((x) => x.provider_id === id);
  return dead ? `${base}（${dead.status_text}）` : base;
}

const PERIODS: { value: "day" | "month" | "total"; label: string }[] = [
  { value: "day", label: "每日" },
  { value: "month", label: "每月" },
  { value: "total", label: "累计" },
];

const periodLabel: Record<string, string> = { day: "每日", month: "每月", total: "累计" };

function fmtNum(n: number | null | undefined): string {
  const v = Number(n || 0);
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(2) + "M";
  if (v >= 1_000) return (v / 1_000).toFixed(1) + "K";
  return String(v);
}

async function load() {
  loading.value = true;
  try {
    const [lv, glob, all, prov] = await Promise.all([
      apiLevels(),
      apiGet<{ items: QuotaRow[] }>("/quota?scope_type=global"),
      apiGet<{ items: QuotaRow[] }>("/quota"),
      apiProviders().catch(() => ({ items: [], circuit_open: [], route_enabled: true })),
    ]);
    levels.value = lv.items || [];
    staleRefs.value = lv.stale || [];
    staleTotal.value = lv.stale_total || 0;
    configuredKnown.value = lv.configured_known !== false;
    globalRows.value = glob.items || [];
    // 对象专属额度：好友 / 群 / 群成员（member 的 scope_id 是「群号:QQ」）
    specificRows.value = (all.items || []).filter(
      (r) => r.scope_type === "user" || r.scope_type === "group" || r.scope_type === "member",
    );
    providers.value = prov.items || [];
    circuitOpen.value = prov.circuit_open || [];
    defaultProviderId.value = (prov as any).default_id || "";
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    loading.value = false;
  }
}

// ------------------------------------------------------------------ //
// 全局默认额度（v1.4.0：私聊 / 群聊各一份）
// ------------------------------------------------------------------ //
type GlobalQuotaForm = Record<string, { limit: number | null; mode: "enforce" | "observe" }>;

// 表单值：null = 不配置（该周期没有额度）；0 = 明确不限
function blankSceneForm(): GlobalQuotaForm {
  return {
    day: { limit: null, mode: "enforce" },
    month: { limit: null, mode: "enforce" },
    total: { limit: null, mode: "enforce" },
  };
}

const SCENES: { value: "private" | "group"; label: string; hint: string }[] = [
  { value: "private", label: "私聊默认", hint: "按「人」记账：每个好友自己的用量" },
  { value: "group", label: "群聊默认", hint: "按「群」记账：每个群自己的用量（含群成员级额度兜底）" },
];

const gForm = ref<{ private: GlobalQuotaForm; group: GlobalQuotaForm }>({
  private: blankSceneForm(),
  group: blankSceneForm(),
});

/** 某个场景的全局额度行（scope_id = private / group；迁移前的旧 '*' 行闸门已不认） */
function rowsOfScene(scene: string): QuotaRow[] {
  return globalRows.value.filter((r) => (r.scope_id || "private") === scene);
}

function rowOfScenePeriod(scene: string, period: string): QuotaRow | undefined {
  return rowsOfScene(scene).find((r) => r.period === period);
}

function fillGlobalForm() {
  for (const s of SCENES) {
    const fresh = blankSceneForm();
    for (const p of PERIODS) {
      const row = rowOfScenePeriod(s.value, p.value);
      fresh[p.value] = row
        ? { limit: row.limit_tokens, mode: row.mode as "enforce" | "observe" }
        : { limit: null, mode: "enforce" };
    }
    gForm.value[s.value] = fresh;
  }
}

async function saveGlobal(scene: "private" | "group") {
  const items: Record<string, unknown>[] = [];
  for (const p of PERIODS) {
    const cur = rowOfScenePeriod(scene, p.value);
    const want = gForm.value[scene][p.value];
    if (want.limit == null) {
      if (cur) items.push({ scope_type: "global", scope_id: scene, period: p.value, limit_tokens: null });
      continue;
    }
    if (!cur || cur.limit_tokens !== want.limit || cur.mode !== want.mode) {
      items.push({ scope_type: "global", scope_id: scene, period: p.value, limit_tokens: want.limit, mode: want.mode });
    }
  }
  if (!items.length) {
    message.info("全局额度没有变化");
    return;
  }
  saving.value = true;
  try {
    await apiPost("/quota", { items });
    message.success("全局额度已保存");
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    saving.value = false;
  }
}

// ------------------------------------------------------------------ //
// 等级管理
// ------------------------------------------------------------------ //
const showLevelEditor = ref(false);
const levelSaving = ref(false);
const levelForm = ref<
  Required<Omit<LevelPayload, "quotas">> & {
    quotas: Record<string, { limit: number | null; mode: "enforce" | "observe" }>;
  }
>({
  id: 0,
  kind: "user",
  name: "",
  description: "",
  effect: "inherit",
  effect_command: "inherit",
  effect_group: "inherit",
  command_effect_group: "inherit",
  sort_order: 0,
  provider_id: "",
  fallback_provider_id: "",
  switch_enabled: false,
  detect_enabled: false,
  switch_providers: [],
  quotas: { day: { limit: null, mode: "enforce" }, month: { limit: null, mode: "enforce" }, total: { limit: null, mode: "enforce" } },
});

  function openLevelEditor(row?: LevelRow) {
  if (row) {
  levelForm.value = {
  id: row.id,
  kind: row.kind,
  name: row.name,
  description: row.description || "",
  effect: row.effect || "inherit",
  effect_command: row.effect_command || "inherit",
  effect_group: row.effect_group || "inherit",
  command_effect_group: row.command_effect_group || "inherit",
  sort_order: row.sort_order || 0,
  provider_id: row.provider_id || "",
  fallback_provider_id: row.fallback_provider_id || "",
  switch_enabled: !!row.switch_enabled,
  detect_enabled: !!row.detect_enabled,
  switch_providers: [...(row.switch_providers || [])],
      quotas: {
        day: row.quotas?.day ? { limit: row.quotas.day.limit_tokens, mode: row.quotas.day.mode as "enforce" | "observe" } : { limit: null, mode: "enforce" },
        month: row.quotas?.month ? { limit: row.quotas.month.limit_tokens, mode: row.quotas.month.mode as "enforce" | "observe" } : { limit: null, mode: "enforce" },
        total: row.quotas?.total ? { limit: row.quotas.total.limit_tokens, mode: row.quotas.total.mode as "enforce" | "observe" } : { limit: null, mode: "enforce" },
      },
    };
  } else {
    levelForm.value = {
      id: 0,
      kind: "user",
      name: "",
      description: "",
      effect: "inherit",
      effect_command: "inherit",
      sort_order: (levels.value.length + 1) * 10,
      provider_id: "",
      fallback_provider_id: "",
      switch_enabled: false,
      detect_enabled: false,
      switch_providers: [],
      quotas: { day: { limit: null, mode: "enforce" }, month: { limit: null, mode: "enforce" }, total: { limit: null, mode: "enforce" } },
    };
  }
  showLevelEditor.value = true;
}

async function saveLevel() {
  const f = levelForm.value;
  if (!f.name.trim()) {
    message.warning("请填写等级名称");
    return;
  }
  levelSaving.value = true;
  try {
    const quotas = PERIODS.map((p) => {
      const want = f.quotas[p.value];
      // 留空(null) → 后端删除该周期；0 → 明确「不限」（占住档位）
      return { period: p.value, limit_tokens: want.limit, mode: want.mode };
    });
    await apiSetLevel({
      id: f.id || undefined,
      kind: f.kind,
      name: f.name.trim(),
      description: f.description?.trim() || "",
      effect: f.effect,
      effect_command: f.effect_command,
      effect_group: f.effect_group,
      command_effect_group: f.command_effect_group,
      sort_order: f.sort_order,
      provider_id: f.provider_id || "",
      fallback_provider_id: f.fallback_provider_id || "",
      switch_enabled: !!f.switch_enabled,
      detect_enabled: !!f.detect_enabled,
      switch_providers: f.switch_providers || [],
      quotas,
    });
    message.success("等级已保存");
    showLevelEditor.value = false;
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    levelSaving.value = false;
  }
}

async function removeLevel(row: LevelRow) {
  try {
    await apiDeleteLevel(row.id);
    message.success(`已删除等级「${row.name}」，其中 ${row.members} 个对象回到未分组`);
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

function levelQuotaText(row: LevelRow): string {
  const parts: string[] = [];
  for (const p of PERIODS) {
    const q = row.quotas?.[p.value];
    if (!q) continue;
    parts.push(`${periodLabel[p.value]} ${q.limit_tokens === 0 ? "不限" : fmtNum(q.limit_tokens)}`);
  }
  return parts.length ? parts.join(" / ") : "未配置（继续用更粗的档位）";
}

const effectText: Record<string, string> = { inherit: "继承", allow: "放行", deny: "禁止" };

const levelColumns: DataTableColumns<LevelRow> = [
  {
    title: "等级",
    key: "name",
    minWidth: 150,
    render: (row) =>
      h("div", { style: "line-height:1.35" }, [
        h("div", { style: "font-weight:500" }, row.name),
        row.description ? h("div", { style: "font-size:12px;opacity:.65" }, row.description) : null,
      ]),
  },
  {
    title: "适用",
    key: "kind",
    width: 86,
    render: (row) =>
      h(
        NTag,
        {
          size: "small",
          bordered: false,
          type: row.kind === "group" ? "success" : "info",
          style: "font-weight:500",
        },
        { default: () => (row.kind === "group" ? "群聊" : "私聊") },
      ),
  },
  {
    title: "默认权限",
    key: "effect",
    width: 150,
    render: (row) =>
      h("div", { style: "line-height:1.5" }, [
        h(
          NTooltip,
          { trigger: "hover" },
          {
            trigger: () =>
              h("div", { style: "font-size:12.5px" }, [
                "LLM：",
                h(
                  NTag,
                  { size: "tiny", bordered: false, type: row.effect === "deny" ? "error" : row.effect === "allow" ? "success" : "default" },
                  { default: () => effectText[row.effect] || row.effect },
                ),
              ]),
            default: () =>
              row.effect === "inherit"
                ? "该等级不管 LLM 对话权限，只看更具体的规则与全局默认"
                : `属于该等级的对象默认${effectText[row.effect]}（优先级低于好友/群专属规则）`,
          },
        ),
        h(
          NTooltip,
          { trigger: "hover" },
          {
            trigger: () =>
              h("div", { style: "font-size:12.5px" }, [
                "指令：",
                h(
                  NTag,
                  { size: "tiny", bordered: false, type: row.effect_command === "deny" ? "error" : row.effect_command === "allow" ? "success" : "default" },
                  { default: () => effectText[row.effect_command || "inherit"] || "继承" },
                ),
              ]),
            default: () =>
              row.effect_command === "deny"
                ? "该等级下的好友 / 群不能用任何指令（可在「私聊 / 群聊」页给个别人开白名单）"
                : row.effect_command === "allow"
                  ? "该等级默认允许使用指令"
                  : "该等级不管指令权限，按更具体的规则与「指令默认策略」走",
          },
        ),
        // 好友等级可以单独配「群聊」那一套，配了就额外标一行
        row.kind === "user" &&
        ((row.effect_group && row.effect_group !== "inherit") ||
          (row.command_effect_group && row.command_effect_group !== "inherit"))
          ? h(NTooltip, { trigger: "hover" }, {
              trigger: () =>
                h("div", { style: "font-size:11.5px;opacity:.7" }, [
                  "群聊：" +
                    (row.effect_group === "inherit"
                      ? "跟随"
                      : effectText[row.effect_group] || row.effect_group) +
                    " / " +
                    (row.command_effect_group === "inherit"
                      ? "跟随"
                      : effectText[row.command_effect_group] || row.command_effect_group),
                ]),
              default: () =>
                "该好友等级在**群聊**里的默认权限（与私聊那一套分开配）：「跟随」= 用私聊设置",
            })
          : null,
      ]),
  },
  {
    title: "模型",
    key: "provider_id",
    minWidth: 190,
    render: (row) => {
      if (!row.provider_id && !row.fallback_provider_id) {
        return h(NTag, { size: "small", bordered: false }, { default: () => "跟随 AstrBot 默认" });
      }
      const main = row.provider_id ? providerLabel(row.provider_id) : "未配置";
      const fb = row.fallback_provider_id ? providerLabel(row.fallback_provider_id) : "";
      const bad = row.stale_fields || [];
      const modelStale = bad.includes("provider_id") || bad.includes("fallback_provider_id");
      return h(
        NTooltip,
        { trigger: "hover" },
        {
          trigger: () =>
            h("div", { style: "line-height:1.35" }, [
              h("div", { style: "font-size:12.5px" }, [
                main,
                modelStale
                  ? h(NTag, { size: "tiny", type: "error", bordered: false, style: "margin-left:6px" }, { default: () => "已失效" })
                  : null,
              ]),
              fb ? h("div", { style: "font-size:12px;opacity:.65" }, `备用：${fb}`) : null,
            ]),
          default: () =>
            `主模型：${main}\n备用模型：${fb || "（未配置）"}` +
            (modelStale ? "\n\n有模型已被删除或禁用，运行时会回落 AstrBot 默认；可在页顶「失效模型引用」里清理。" : ""),
        },
      );
    },
  },
  {
    // 自助切换（v9/v10）+ 检测（v11）：开关 + 该等级的用户发 /切换模型 时能挑的范围
    title: "自助切换",
    key: "switch_providers",
    minWidth: 180,
    render: (row) => {
      const list = row.switch_providers || [];
      const names = list.length
        ? list.map((id) => providerLabel(id))
        : ["当前使用的模型", "系统默认模型", "备用模型"];
      // 切换：只读 / 允许（带可挑范围）
      const switchNode = row.switch_enabled
        ? h(
            NTooltip,
            { trigger: "hover" },
            {
              trigger: () =>
                h("div", { style: "line-height:1.35;cursor:default" }, [
                  h(
                    NTag,
                    { size: "small", bordered: false, type: "success" },
                    { default: () => (list.length ? `允许 · ${list.length} 个` : "允许 · 兜底三项") },
                  ),
                  h("div", { style: "font-size:12px;opacity:.65" }, names.join("、")),
                ]),
              default: () =>
                (list.length ? "用户发 /切换模型 时可选：\n" : "未配名单，用户可在兜底三项里选：\n") +
                names.map((n, i) => `${i + 1}. ${n}`).join("\n"),
            },
          )
        : h(
            NTooltip,
            { trigger: "hover" },
            {
              trigger: () =>
                h(NTag, { size: "small", bordered: false }, { default: () => "只读（默认）" }),
              default: () =>
                "该等级的用户发 /切换模型 只能查看「当前使用 / 系统默认 / 备用」，不能切换",
            },
          );
      // 检测（v11）：与上面独立 —— 它是花钱的，所以单独一个标签
      const detectNode = h(
        NTooltip,
        { trigger: "hover" },
        {
          trigger: () =>
            h(
              NTag,
              { size: "small", bordered: false, type: row.detect_enabled ? "info" : "default" },
              { default: () => (row.detect_enabled ? "可用检测" : "检测关闭") },
            ),
          default: () =>
            row.detect_enabled
              ? "该等级的用户可以发 /切换模型检测（真打模型、会花额度；需要装萌萌模型控制台）"
              : "该等级的用户不能用 /切换模型检测（默认关；打开要在这个等级的编辑里勾）",
        },
      );
      return h("div", { style: "line-height:1.35" }, [
        switchNode,
        h("div", { style: "margin-top:4px" }, [detectNode]),
      ]);
    },
  },
  { title: "额度模板", key: "quotas", minWidth: 200, render: (row) => levelQuotaText(row) },
  {
    title: "操作",
    key: "actions",
    width: 130,
    render: (row) =>
      h(NSpace, { size: 6 }, {
        default: () => [
          h(NButton, { size: "tiny", onClick: () => openLevelEditor(row) }, { default: () => "编辑" }),
          h(NButton, { size: "tiny", type: "error", ghost: true, onClick: () => removeLevel(row) }, { default: () => "删除" }),
        ],
      }),
  },
];

// ------------------------------------------------------------------ //
// 对象专属额度
// ------------------------------------------------------------------ //
const showQuotaEditor = ref(false);
const quotaSaving = ref(false);
const quotaForm = ref({
  scope_type: "user" as "user" | "group" | "member",
  scope_id: "",
  period: "day" as "day" | "month" | "total",
  limit_tokens: 100000,
  mode: "enforce" as "enforce" | "observe",
});

function openQuotaEditor(row?: QuotaRow) {
  quotaForm.value = row
    ? {
        scope_type: row.scope_type as "user" | "group" | "member",
        scope_id: row.scope_id,
        period: row.period,
        limit_tokens: row.limit_tokens,
        mode: row.mode,
      }
    : { scope_type: "user", scope_id: "", period: "day", limit_tokens: 100000, mode: "enforce" };
  showQuotaEditor.value = true;
}

async function saveQuota() {
  const sid = quotaForm.value.scope_id.trim();
  if (!sid) {
    message.warning("请填写 QQ 号或群号");
    return;
  }
  if (quotaForm.value.scope_type === "member") {
    const [gid, uid] = sid.split(":");
    if (!gid || !uid) {
      message.warning("群成员额度要填「群号:QQ」，例如 123456789:987654321");
      return;
    }
  }
  quotaSaving.value = true;
  try {
    await apiPost("/quota", { ...quotaForm.value, scope_id: quotaForm.value.scope_id.trim() });
    message.success("已保存");
    showQuotaEditor.value = false;
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  } finally {
    quotaSaving.value = false;
  }
}

async function resetUsed(row: QuotaRow) {
  try {
    await apiPost("/quota/reset", {
      scope_type: row.scope_type,
      scope_id: row.scope_id,
      period: row.period,
    });
    message.success("已清零该周期的用量");
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

async function removeQuota(row: QuotaRow) {
  try {
    await apiPost("/quota", { scope_type: row.scope_type, scope_id: row.scope_id, period: row.period, limit_tokens: null });
    message.success("已删除该条限额");
    await load();
  } catch (e: any) {
    message.error(e?.message || String(e));
  }
}

/** 成员额度的 scope_id 是「群号:QQ」，展示成人话才看得懂。 */
function memberLabel(scopeId: string): string {
  const [gid, uid] = String(scopeId || "").split(":");
  return `群 ${gid} 的成员 ${uid}`;
}

function pct(row: QuotaRow): number {
  if (!row.limit_tokens) return 0;
  return Math.min(100, Math.round((Number(row.used_tokens || 0) / row.limit_tokens) * 100));
}

const quotaColumns: DataTableColumns<QuotaRow> = [
  {
    title: "对象",
    key: "scope_id",
    minWidth: 160,
    render: (row) => {
      const [kindText, idText] =
        row.scope_type === "member"
          ? ["群成员", memberLabel(row.scope_id)]
          : [row.scope_type === "user" ? "QQ 好友" : "群聊", row.scope_id];
      return h("div", { style: "line-height:1.3" }, [
        h("div", { style: "font-weight:500" }, idText),
        h("div", { style: "font-size:12px;opacity:.65" }, kindText),
      ]);
    },
  },
  { title: "周期", key: "period", width: 80, render: (row) => periodLabel[row.period] || row.period },
  {
    title: "模式",
    key: "mode",
    width: 90,
    render: (row) =>
      h(
        NTag,
        { size: "small", type: row.mode === "observe" ? "warning" : "success", bordered: false },
        { default: () => (row.mode === "observe" ? "观察" : "拦截") },
      ),
  },
  {
    title: "用量",
    key: "used_tokens",
    minWidth: 200,
    render: (row) =>
      h("div", { style: "min-width:170px" }, [
        h("div", { style: "font-size:12px;margin-bottom:2px" }, `${fmtNum(row.used_tokens)} / ${fmtNum(row.limit_tokens)}`),
        h(NProgress, { type: "line", percentage: pct(row), height: 6, showIndicator: false, status: (row.used_tokens || 0) >= row.limit_tokens ? "error" : undefined }),
      ]),
  },
  {
    title: "重置时间",
    key: "reset_at",
    width: 150,
    render: (row) =>
      row.reset_at ? new Date(row.reset_at * 1000).toLocaleString() : row.period === "total" ? "不重置" : "-",
  },
  {
    title: "操作",
    key: "actions",
    width: 160,
    render: (row) =>
      h(NSpace, { size: 6 }, {
        default: () => [
          h(NButton, { size: "tiny", onClick: () => openQuotaEditor(row) }, { default: () => "编辑" }),
          h(NButton, { size: "tiny", quaternary: true, onClick: () => resetUsed(row) }, { default: () => "清零" }),
          h(NButton, { size: "tiny", type: "error", ghost: true, onClick: () => removeQuota(row) }, { default: () => "删除" }),
        ],
      }),
  },
];

// 等级按 kind 拆成两张表（私聊 / 群聊），数据一次拉全，这里只是前端分组
const userLevels = computed(() => levels.value.filter((l) => l.kind === "user"));
const groupLevels = computed(() => levels.value.filter((l) => l.kind === "group"));
const levelCount = computed(() => levels.value.length);

onMounted(load);
</script>

<template>
  <n-space vertical :size="14">
    <!-- AstrBot 删除 / 禁用模型后，库里残留的旧 id：只提示，清理必须用户主动点 -->
    <n-alert v-if="staleTotal > 0" type="warning" :bordered="false">
      <template #header>发现 {{ staleTotal }} 个模型配置已失效</template>
      <div style="font-size: 13px; line-height: 1.6">
        <div>
          AstrBot 里部分模型已被删除、禁用或尚未加载，等级路由 / 可切换名单 / 专属模型里仍引用着它们。
          这些配置不会生效（请求会回落到 AstrBot 默认模型），插件也<b>不会自动删除</b>——
          禁用是临时可逆的，重新启用后原来的配置还能照常工作。
        </div>
        <div v-if="!configuredKnown" style="margin-top: 4px; opacity: 0.8">
          当前读不到 AstrBot 的模型配置，无法区分「已删除」与「暂时未加载」，已暂停清理入口。
        </div>
        <n-space align="center" :size="10" style="margin-top: 8px">
          <n-button size="small" type="warning" secondary @click="openStaleModal">
            查看并处理
          </n-button>
          <span style="font-size: 12px; opacity: 0.7">历史用量与审计日志会保留，不受影响</span>
        </n-space>
      </div>
    </n-alert>

    <n-card size="small" title="额度说明">
      <n-space vertical :size="4" style="font-size: 13px; opacity: 0.82">
        <span>· 额度口径 = 输入 token + 缓存 token + 输出 token（「缓存计入」开关在配置页）。</span>
        <span>
          · <b>全局默认额度分「私聊默认」与「群聊默认」两份</b>（v1.4.0）：
          私聊按「人」记账、群聊按「群」记账，互不影响，都在「额度模板」之上的最后一层兜底。
        </span>
        <span>
          · 解析优先级：<b>好友专属 → 好友等级 → 群专属 → 群等级 → 全局</b>，
          <b>命中最具体的一层即止</b>——所以「VIP 等级 50 万」不会被「全局 5 万」反手拦掉。
        </span>
        <span>· 命中那一层内部，日 / 月 / 累计同时生效，任何一个超限就拦（模式为「观察」时只记账不拦）。</span>
        <span>· 留空 = 该周期不配置（继续看更粗的档位）；<b>填 0 = 明确不限</b>（占住档位，更粗的不再参与）。</span>
        <span>· 用量按「每个对象自己」记账（与是否配置额度无关），等级额度是「每人一份」，不是整组合计。</span>
        <span>
          · <b>模型路由</b>：等级可指定「主模型 + 备用模型」。<b>私聊看好友等级、群聊看群等级</b>
          （一个群只用一个模型，避免同群上下文串味）；主提供商不可用或连续失败熔断时自动走备用。
        </span>
        <span>
          · <b>自助切换模型</b>：等级里的「允许切换模型」开关（<b>默认关</b>）打开后，属于该等级的用户发
          <b>/切换模型</b> 就能自助换模型——卡片列出候选并标出<b>当前使用的那个</b>，回序号即切换、回
          <b>0</b> 恢复默认；开关关着则是<b>只读</b>（卡片照发，但切不了）。
          候选 = 等级配的「可切换模型」名单，没配就是<b>兜底三项</b>（当前使用 / 系统默认 / 备用）。
          <b>群聊里只有管理员能切</b>（群共用一个会话，谁都能切会把整个群搅乱），
          <b>管理员不受开关与群聊限制</b>。优先级：<b>用户自己的选择 &gt; 专属模型 &gt; 等级主/备用模型</b>。
        </span>
      </n-space>
    </n-card>

    <!-- 全局默认额度：私聊 / 群聊各一份（v1.4.0） -->
    <n-grid cols="1 m:2" responsive="screen" :x-gap="14" :y-gap="14">
      <n-grid-item v-for="s in SCENES" :key="s.value">
        <n-card size="small">
          <template #header>
            <n-space align="center" :size="10">
              <n-tag size="small" :bordered="false" :type="s.value === 'group' ? 'success' : 'info'" style="font-weight: 500">
                {{ s.label }}
              </n-tag>
              <span style="font-size: 12.5px; opacity: 0.65">{{ s.hint }}</span>
            </n-space>
          </template>
          <n-space vertical :size="10">
            <n-space v-for="p in PERIODS" :key="p.value" align="center" :size="10">
              <span style="width: 56px; font-size: 13px">{{ p.label }}</span>
              <n-input-number
                v-model:value="gForm[s.value][p.value].limit"
                size="small"
                :step="10000"
                placeholder="留空 = 不配置"
                style="width: 190px"
              />
              <n-radio-group v-model:value="gForm[s.value][p.value].mode" size="small">
                <n-radio-button value="enforce">拦截</n-radio-button>
                <n-radio-button value="observe">观察</n-radio-button>
              </n-radio-group>
              <n-tag
                v-if="rowOfScenePeriod(s.value, p.value)"
                size="small"
                :bordered="false"
                :type="rowOfScenePeriod(s.value, p.value)!.limit_tokens === 0 ? 'default' : 'info'"
              >
                当前：{{ rowOfScenePeriod(s.value, p.value)!.limit_tokens === 0 ? "不限" : fmtNum(rowOfScenePeriod(s.value, p.value)!.limit_tokens) }}
              </n-tag>
              <n-tag v-else size="small" :bordered="false">未配置</n-tag>
            </n-space>
            <n-space align="center">
              <n-button size="small" type="primary" :loading="saving" @click="saveGlobal(s.value)">
                保存{{ s.label }}额度
              </n-button>
              <span style="font-size: 12px; opacity: 0.6">
                没有专属额度、也没有等级的对象按这份算
              </span>
            </n-space>
          </n-space>
        </n-card>
      </n-grid-item>
    </n-grid>

    <!-- 等级管理：私聊 / 群聊两段，颜色区分 -->
    <n-card size="small">
      <template #header>
        <n-space align="center" :size="10">
          <span>等级管理</span>
          <n-tag size="small" :bordered="false">{{ levelCount }} 个等级</n-tag>
        </n-space>
      </template>
      <template #header-extra>
        <n-button size="small" quaternary @click="load">刷新</n-button>
      </template>

      <!-- 私聊等级 -->
      <div class="level-section">
        <div class="level-section-head level-section--user">
          <span class="level-section-title">私聊等级</span>
          <span class="level-section-sub">作用于好友私聊（蓝）</span>
          <n-button size="tiny" class="level-section-add" @click="openLevelEditor()">+ 新增私聊等级</n-button>
        </div>
        <n-empty
          v-if="!loading && !userLevels.length"
          description="还没有私聊等级 —— 可以先建「普通 / VIP」两档试试"
          style="padding: 22px 0"
        />
        <n-data-table v-else :columns="levelColumns" :data="userLevels" :loading="loading" :bordered="false" size="small" :scroll-x="960" />
      </div>

      <!-- 群聊等级 -->
      <div class="level-section" style="margin-top: 18px">
        <div class="level-section-head level-section--group">
          <span class="level-section-title">群聊等级</span>
          <span class="level-section-sub">作用于群聊（绿），只看群不看人</span>
          <n-button size="tiny" class="level-section-add" @click="openLevelEditor({ id: 0, kind: 'group', name: '', description: '', effect: 'inherit', sort_order: 0, members: 0, quotas: {} } as any)">
            + 新增群聊等级
          </n-button>
        </div>
        <n-empty
          v-if="!loading && !groupLevels.length"
          description="还没有群聊等级"
          style="padding: 22px 0"
        />
        <n-data-table v-else :columns="levelColumns" :data="groupLevels" :loading="loading" :bordered="false" size="small" :scroll-x="960" />
      </div>
    </n-card>

    <!-- 对象专属额度 -->
    <n-card size="small">
      <template #header>
        <n-space align="center" :size="10">
          <span>对象专属额度</span>
          <n-tag size="small" :bordered="false">{{ specificRows.length }} 条</n-tag>
        </n-space>
      </template>
      <template #header-extra>
        <n-button size="small" type="primary" @click="openQuotaEditor()">新增专属额度</n-button>
      </template>
      <n-empty v-if="!loading && !specificRows.length" description="还没有对象专属额度" style="padding: 30px 0" />
      <n-data-table v-else :columns="quotaColumns" :data="specificRows" :loading="loading" :bordered="false" size="small" :scroll-x="520" />
    </n-card>

    <!-- 等级编辑弹窗：按「基本 / 权限 / 模型 / 额度」分四个标签页，避免长表单拥挤 -->
    <n-modal v-model:show="showLevelEditor" preset="card" :title="levelForm.id ? '编辑等级' : '新增等级'" style="width: 860px; max-width: 94vw">
      <n-tabs type="line" animated>
        <n-tab-pane name="base" tab="基本信息">
          <n-form label-placement="left" label-width="110" style="padding-top: 6px">
            <n-form-item label="适用">
              <n-space align="center" :size="10">
                <n-radio-group v-model:value="levelForm.kind" :disabled="!!levelForm.id">
                  <n-radio-button value="user">私聊（好友）</n-radio-button>
                  <n-radio-button value="group">群聊（群）</n-radio-button>
                </n-radio-group>
                <span style="font-size: 12px; opacity: 0.6">创建后不可更改</span>
              </n-space>
            </n-form-item>
            <n-form-item label="等级名称">
              <n-input v-model:value="levelForm.name" placeholder="如：普通 / VIP / 黑名单" style="max-width: 380px" />
            </n-form-item>
            <n-form-item label="说明">
              <n-input v-model:value="levelForm.description" placeholder="可选，给自己看的备注" style="max-width: 380px" />
            </n-form-item>
            <n-form-item label="排序值">
              <n-space align="center" :size="10">
                <n-input-number v-model:value="levelForm.sort_order" size="small" style="width: 160px" />
                <span style="font-size: 12px; opacity: 0.6">仅决定控制台里的展示顺序</span>
              </n-space>
            </n-form-item>
          </n-form>
        </n-tab-pane>

        <n-tab-pane name="permission" tab="默认权限">
          <n-form label-placement="left" label-width="110" style="padding-top: 6px">
            <n-form-item :label="levelForm.kind === 'user' ? '私聊 LLM 权限' : '默认 LLM 权限'">
              <n-space align="center" :size="10">
                <n-radio-group v-model:value="levelForm.effect">
                  <n-radio-button value="inherit">继承</n-radio-button>
                  <n-radio-button value="allow">放行</n-radio-button>
                  <n-radio-button value="deny">禁止</n-radio-button>
                </n-radio-group>
                <span style="font-size: 12px; opacity: 0.6">「继承」= 该等级不管 LLM 对话权限</span>
              </n-space>
            </n-form-item>
            <n-form-item :label="levelForm.kind === 'user' ? '私聊指令权限' : '默认指令权限'">
              <n-space vertical :size="6" style="width: 100%">
                <n-radio-group v-model:value="levelForm.effect_command">
                  <n-radio-button value="inherit">继承</n-radio-button>
                  <n-radio-button value="allow">放行</n-radio-button>
                  <n-radio-button value="deny">禁止</n-radio-button>
                </n-radio-group>
                <span style="font-size: 12px; opacity: 0.6; line-height: 1.6">
                  与 LLM 权限<b>相互独立</b>：设成「禁止」= 该等级下的好友 / 群<b>不能用任何指令</b>；
                  要给个别人开白名单，去「私聊 / 群聊」页把那个人的指令权限设为「放行」。
                </span>
              </n-space>
            </n-form-item>
            <n-form-item v-if="levelForm.kind === 'user'" label="群聊默认权限">
              <n-space vertical :size="8" style="width: 100%">
                <n-space align="center" :size="8">
                  <span style="font-size: 12.5px; width: 34px">LLM</span>
                  <n-radio-group v-model:value="levelForm.effect_group">
                    <n-radio-button value="inherit">跟随私聊</n-radio-button>
                    <n-radio-button value="allow">放行</n-radio-button>
                    <n-radio-button value="deny">禁止</n-radio-button>
                  </n-radio-group>
                </n-space>
                <n-space align="center" :size="8">
                  <span style="font-size: 12.5px; width: 34px">指令</span>
                  <n-radio-group v-model:value="levelForm.command_effect_group">
                    <n-radio-button value="inherit">跟随私聊</n-radio-button>
                    <n-radio-button value="allow">放行</n-radio-button>
                    <n-radio-button value="deny">禁止</n-radio-button>
                  </n-radio-group>
                </n-space>
                <span style="font-size: 12px; opacity: 0.6; line-height: 1.6">
                  好友等级在群里同样生效，所以能单独说「私聊禁止、群里照用」：
                  这里设「禁止」只影响该等级的人在<b>群聊</b>里的行为；
                  「跟随私聊」= 用上面私聊那一套（旧数据就是这个状态，行为不变）。
                </span>
              </n-space>
            </n-form-item>
          </n-form>
        </n-tab-pane>

        <n-tab-pane name="model" tab="模型与自助">
          <n-form label-placement="left" label-width="110" style="padding-top: 6px">
            <n-form-item label="主模型">
              <n-select
                v-model:value="levelForm.provider_id"
                size="small"
                style="max-width: 420px"
                clearable
                filterable
                placeholder="搜索并选择模型（留空 = 跟随 AstrBot 默认）"
                :options="providerOptions"
              />
            </n-form-item>
            <n-form-item label="备用模型">
              <n-space vertical :size="4" style="width: 100%">
                <n-select
                  v-model:value="levelForm.fallback_provider_id"
                  size="small"
                  style="max-width: 420px"
                  clearable
                  filterable
                  placeholder="主模型不可用时改用它（可选）"
                  :options="providerOptions"
                />
                <span style="font-size: 12px; opacity: 0.6; line-height: 1.6">
                  选项就是「供应商 · 模型」。私聊按「好友等级」、群聊按「群等级」决定模型；
                  主模型未加载或连续失败（熔断）时自动落到备用。
                </span>
              </n-space>
            </n-form-item>
            <n-form-item label="允许切换模型">
              <n-space vertical :size="4" style="width: 100%">
                <n-space align="center" :size="10">
                  <n-switch v-model:value="levelForm.switch_enabled" size="small" />
                  <n-tag size="small" :bordered="false" :type="levelForm.switch_enabled ? 'success' : 'default'">
                    {{ levelForm.switch_enabled ? "用户可自助切换" : "只读（默认）：能看不能切" }}
                  </n-tag>
                </n-space>
                <span style="font-size: 12px; opacity: 0.6; line-height: 1.6">
                  关掉时该等级的用户发 <b>/切换模型</b> 仍然能看到卡片，但<b>回序号不生效</b>。
                  <b>管理员不受限制</b>；<b>群里只有管理员能切</b>。
                </span>
              </n-space>
            </n-form-item>
            <n-form-item label="可切换模型">
              <n-space vertical :size="4" style="width: 100%">
                <n-select
                  v-model:value="levelForm.switch_providers"
                  multiple
                  filterable
                  size="small"
                  style="max-width: 420px"
                  :disabled="!levelForm.switch_enabled"
                  placeholder="留空 = 用兜底三项（当前 / 系统默认 / 备用）"
                  :options="providerOptions"
                />
                <span style="font-size: 12px; opacity: 0.6; line-height: 1.6">
                  只有上面打开了开关才有意义。填了就<b>只能从这份名单里挑</b>（外加他自己的专属模型）；
                  用户随时可以发「/切换模型 0」回到这里配的主模型。
                </span>
              </n-space>
            </n-form-item>
            <n-form-item label="允许检测模型">
              <n-space vertical :size="4" style="width: 100%">
                <n-space align="center" :size="10">
                  <n-switch v-model:value="levelForm.detect_enabled" size="small" />
                  <n-tag size="small" :bordered="false" :type="levelForm.detect_enabled ? 'success' : 'default'">
                    {{ levelForm.detect_enabled ? "可用 /切换模型检测" : "不可用（默认）" }}
                  </n-tag>
                </n-space>
                <span style="font-size: 12px; opacity: 0.6; line-height: 1.6">
                  打开后该等级的用户可以发 <b>/切换模型检测</b>（真打模型、<b>会花额度</b>，
                  与「允许切换模型」分开两个开关）。需要先安装「<b>萌萌模型控制台</b>」；
                  <b>管理员不受限制</b>，非管理员另有冷却（配置页「模型检测」分区可调）。
                </span>
              </n-space>
            </n-form-item>
          </n-form>
        </n-tab-pane>

        <n-tab-pane name="quota" tab="额度模板">
          <n-form label-placement="left" label-width="110" style="padding-top: 6px">
            <n-form-item label="额度模板">
              <n-space vertical :size="10" style="width: 100%">
                <n-space v-for="p in PERIODS" :key="p.value" align="center" :size="8">
                  <span style="width: 44px; font-size: 13px">{{ p.label }}</span>
                  <n-input-number
                    v-model:value="levelForm.quotas[p.value].limit"
                    size="small"
                    :step="10000"
                    placeholder="留空 = 不配置"
                    style="width: 190px"
                  />
                  <n-radio-group v-model:value="levelForm.quotas[p.value].mode" size="small">
                    <n-radio-button value="enforce">拦截</n-radio-button>
                    <n-radio-button value="observe">观察</n-radio-button>
                  </n-radio-group>
                </n-space>
                <span style="font-size: 12px; opacity: 0.6; line-height: 1.6">
                  每个属于该等级的对象各自一份额度（不是整组合计）；填 0 表示明确不限。
                </span>
              </n-space>
            </n-form-item>
          </n-form>
        </n-tab-pane>
      </n-tabs>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showLevelEditor = false">取消</n-button>
          <n-button type="primary" :loading="levelSaving" @click="saveLevel">保存</n-button>
        </n-space>
      </template>
    </n-modal>

    <!-- 专属额度编辑弹窗 -->
    <n-modal v-model:show="showQuotaEditor" preset="card" title="专属额度" style="width: 460px; max-width: 94vw">
      <n-form label-placement="left" label-width="92">
        <n-form-item label="对象类型">
          <n-radio-group v-model:value="quotaForm.scope_type">
            <n-radio-button value="user">QQ 好友</n-radio-button>
            <n-radio-button value="group">群聊</n-radio-button>
            <n-radio-button value="member">群成员</n-radio-button>
          </n-radio-group>
        </n-form-item>
        <n-form-item
          :label="quotaForm.scope_type === 'user' ? 'QQ 号' : quotaForm.scope_type === 'group' ? '群号' : '群号:QQ'"
        >
          <n-input
            v-model:value="quotaForm.scope_id"
            :placeholder="quotaForm.scope_type === 'member' ? '如 123456789:987654321（群号:QQ）' : '如 123456789'"
          />
          <span v-if="quotaForm.scope_type === 'member'" style="font-size: 12px; opacity: 0.6">
            只统计他在这个群里的用量，是「群成员级」额度
          </span>
        </n-form-item>
        <n-form-item label="周期">
          <n-select
            v-model:value="quotaForm.period"
            :options="PERIODS.map((p) => ({ label: p.label, value: p.value }))"
          />
        </n-form-item>
        <n-form-item label="额度(token)">
          <n-input-number v-model:value="quotaForm.limit_tokens" :min="0" :step="10000" style="width: 100%" />
        </n-form-item>
        <n-form-item label="模式">
          <n-radio-group v-model:value="quotaForm.mode">
            <n-radio-button value="enforce">拦截</n-radio-button>
            <n-radio-button value="observe">观察</n-radio-button>
          </n-radio-group>
        </n-form-item>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showQuotaEditor = false">取消</n-button>
          <n-button type="primary" :loading="quotaSaving" @click="saveQuota">保存</n-button>
        </n-space>
      </template>
    </n-modal>

    <!-- 失效模型引用：每个 id 的状态 + 出现位置，勾选后才清理 -->
    <n-modal v-model:show="showStaleModal" preset="card" title="失效模型引用" style="width: 720px; max-width: 94vw">
      <n-spin :show="staleLoading">
        <n-space vertical :size="10">
          <n-alert type="info" :bordered="false" style="font-size: 12.5px">
            插件不会自动删除这些配置。已禁用 / 未加载的模型随时可能重新启用，
            清理后原配置会回落为「跟随 AstrBot 默认」，且不可撤销；历史用量与审计日志保留。
          </n-alert>
          <div v-if="!staleReport?.stale?.length" style="padding: 18px 0; text-align: center; opacity: 0.7">
            没有失效的模型引用
          </div>
          <n-checkbox-group v-else v-model:value="staleChecked" style="width: 100%">
            <n-space vertical :size="10" style="width: 100%">
              <div
                v-for="it in staleReport!.stale"
                :key="it.provider_id"
                style="border: 1px solid rgba(128,128,128,.22); border-radius: 8px; padding: 10px 12px"
              >
                <n-checkbox :value="it.provider_id" :disabled="!configuredKnown">
                  <span style="font-weight: 500">{{ it.provider_id }}</span>
                  <n-tag
                    size="tiny"
                    :type="statusType[it.status] || 'default'"
                    :bordered="false"
                    style="margin-left: 8px"
                  >
                    {{ it.status_text }}
                  </n-tag>
                  <span style="font-size: 12px; opacity: 0.7; margin-left: 8px">
                    {{ it.ref_count }} 处引用
                  </span>
                </n-checkbox>
                <div style="font-size: 12px; opacity: 0.72; line-height: 1.7; margin-top: 6px; padding-left: 24px">
                  <div v-for="(ref, i) in it.refs" :key="i">· {{ ref.label }}</div>
                </div>
              </div>
            </n-space>
          </n-checkbox-group>
        </n-space>
      </n-spin>
      <template #footer>
        <n-space justify="space-between" align="center">
          <span style="font-size: 12px; opacity: 0.7">
            默认只勾选「已删除」的；已禁用 / 未加载的建议保留
          </span>
          <n-space :size="8">
            <n-button @click="showStaleModal = false">关闭</n-button>
            <n-button
              type="error"
              secondary
              :loading="stalePruning"
              :disabled="!configuredKnown || !staleChecked.length"
              @click="pruneStale"
            >
              清理所选（{{ staleChecked.length }}）
            </n-button>
          </n-space>
        </n-space>
      </template>
    </n-modal>

    <!-- 清理二次确认（不依赖 NDialogProvider） -->
    <n-modal
      v-model:show="showPruneConfirm"
      preset="card"
      title="确认清理失效模型引用"
      style="width: 480px; max-width: 94vw"
    >
      <div style="font-size: 13px; line-height: 1.7">
        将从等级的主 / 备用模型、可切换名单和对象专属模型里，摘掉
        <b>{{ staleChecked.length }}</b> 个模型：{{ staleChecked.join("、") }}。
        相关配置会回落为「跟随 AstrBot 默认」，历史用量与审计日志保留。
        <b>此操作不可撤销。</b>
      </div>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showPruneConfirm = false">取消</n-button>
          <n-button type="error" :loading="stalePruning" @click="doPrune">确认清理</n-button>
        </n-space>
      </template>
    </n-modal>
  </n-space>
</template>

<style scoped>
/* 等级管理的私聊 / 群聊分区头：色条 + 标题 + 新增按钮，比一列灰 tag 更好认。
   背景用显式的蓝/绿浅底（不引用 CSS 变量，明暗主题下都够读）。 */
.level-section-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 12px;
  margin-bottom: 10px;
  border-radius: 4px;
  border-left: 4px solid transparent;
}
.level-section--user {
  border-left-color: #4098fc;
  background: rgba(64, 152, 252, 0.1);
}
.level-section--group {
  border-left-color: #36ad6a;
  background: rgba(54, 173, 106, 0.1);
}
.level-section-title {
  font-size: 13.5px;
  font-weight: 600;
}
.level-section--user .level-section-title {
  color: #4098fc;
}
.level-section--group .level-section-title {
  color: #36ad6a;
}
.level-section-sub {
  font-size: 12px;
  opacity: 0.6;
}
.level-section-add {
  margin-left: auto;
}
</style>
