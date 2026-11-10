// API 封装：通过 AstrBot 官方插件 Page 桥接调用插件后端。
// 桥接会正确处理 /api/plug/ 路由与 asset_token 鉴权；**不能**用裸 fetch 相对路径（会 CORS 失败）。
//
// 后端统一返回信封 {code, data, message}：code !== 0 时抛错，页面只需 try/catch 展示 e.message。
//
// bridge 的发现/等待逻辑统一放在 ./bridge（那里同时处理 sandbox iframe 的存储限制）。

import { whenBridgeReady } from "./bridge";

const PAGE_PLUGIN_NAME = "astrbot_plugin_moe_permission";

// 不同 AstrBot 版本对端点前缀的处理略有差异，逐个候选尝试（首个成功即返回）。
function endpointCandidates(routePath: string): string[] {
  const clean = routePath.replace(/^\/+/, "");
  const list = [clean, "/" + clean, `${PAGE_PLUGIN_NAME}/${clean}`, `/${PAGE_PLUGIN_NAME}/${clean}`];
  return [...new Set(list.map((s) => s.replace(/\/+/g, "/")).filter(Boolean))];
}

function isRouteMissing(payload: any): boolean {
  if (!payload || typeof payload !== "object") return false;
  const text = String(payload.error || "") + " " + String(payload.message || "") + " " + String(payload.detail || "");
  return /未找到.*路由|route.*not.*found|not.*found.*route|404/i.test(text);
}

function withTimeout<T>(promise: Promise<T>, ms: number, label: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(label + " 超时（" + ms / 1000 + "s 无响应）")), ms);
  });
  return Promise.race([promise, timeout]).finally(() => {
    if (timer) clearTimeout(timer);
  });
}

/**
 * 解析后端返回。正常情况下 Dashboard 已经吃掉一层信封
 * （成功时只把 `data` 转发过来），这里做防御式解包：
 * 若仍是 `{status:"ok"|"error", data}` / `{code, data}` 形态则再脱一层。
 */
function unwrap(payload: any): any {
  if (payload && typeof payload === "object") {
    if (payload.status === "error") throw new Error(payload.message || "请求失败");
    if (payload.status === "ok" && "data" in payload) return payload.data;
    if (Object.prototype.hasOwnProperty.call(payload, "code")) {
      if (payload.code !== 0) throw new Error(payload.message || "请求失败");
      return payload.data;
    }
  }
  return payload;
}

/** 只有「路由不存在」才值得换下一种端点写法重试；业务错误必须立刻抛出。 */
function isRouteMissingError(e: any): boolean {
  if (isRouteMissing(e)) return true;
  const text = String(e?.message || e?.toString?.() || e || "");
  return /未找到.*路由|route.*not.*found|not.*found.*route|404|not found/i.test(text);
}

async function request(path: string, method: "GET" | "POST", body?: unknown, timeoutMs?: number): Promise<any> {
  const br = await whenBridgeReady();
  const url = new URL(path, "https://astrbot-plugin-page.local/");
  const candidates = endpointCandidates(url.pathname);
  const errors: string[] = [];
  const ms = timeoutMs ?? 15000;

  if (method === "GET") {
    const params = Object.fromEntries(url.searchParams.entries());
    for (const c of candidates) {
      try {
        const p = await withTimeout(br.apiGet(c, Object.keys(params).length ? params : undefined), ms, "GET " + c);
        if (isRouteMissing(p)) {
          errors.push(p.message || p.error || "未找到该路由");
          continue;
        }
        return unwrap(p);
      } catch (e: any) {
        // 业务错误（如参数校验失败）直接抛出，避免把同一个请求打到 4 种端点上
        if (!isRouteMissingError(e)) throw e;
        errors.push(e?.message || String(e));
      }
    }
    throw new Error(errors[0] || "未找到可用的页面 API 路由");
  }

  let payload = body || {};
  try {
    payload = JSON.parse(JSON.stringify(payload));
  } catch {
    /* ignore */
  }
  for (const c of candidates) {
    try {
      const r = await withTimeout(br.apiPost(c, payload), ms, "POST " + c);
      if (isRouteMissing(r)) {
        errors.push(r.message || r.error || "未找到该路由");
        continue;
      }
      return unwrap(r);
    } catch (e: any) {
      // 同上：POST 绝不能因为业务错误被重试多次（会出现重复写库）
      if (!isRouteMissingError(e)) throw e;
      errors.push(e?.message || e?.toString?.() || String(e));
    }
  }
  throw new Error(errors[0] || "未找到可用的页面 API 路由");
}

/** GET，endpoint 形如 "/friends"、"usage?page=1"。 */
export function apiGet<T = any>(endpoint: string, timeoutMs?: number): Promise<T> {
  return request(endpoint, "GET", undefined, timeoutMs) as Promise<T>;
}

/** POST，body 作为 JSON 负载。 */
export function apiPost<T = any>(endpoint: string, body?: unknown, timeoutMs?: number): Promise<T> {
  return request(endpoint, "POST", body || {}, timeoutMs) as Promise<T>;
}

// ---------------------------------------------------------------- 类型
export interface PingInfo {
  plugin: string;
  version: string;
  db_ready: boolean;
  data_dir: string;
  db_path: string;
  server_time: number;
  /** 好友/群同步状态 */
  sync?: { last_at: number; running: boolean; ok: boolean; error: string };
  /** 内存中已加载的规则数量（确认改动是否真的生效） */
  rules?: {
    effect_users: number;
    effect_groups: number;
    effect_members?: number;
    levels?: number;
    leveled_users?: number;
    leveled_groups?: number;
    limits?: number;
    usage_users?: number;
    usage_groups?: number;
    usage_members?: number;
  };
  /** 头像缓存概况 */
  avatars?: Record<string, any>;
  /** 指令权限概况 */
  commands?: {
    enabled: boolean;
    default_effect: "allow" | "deny";
    priority: number;
    rules: number;
  };
  /** 模型路由概况（熔断中的提供商会列在这里） */
  model_route?: {
    enabled: boolean;
    levels: number;
    routed_sessions: number;
    circuit: Record<string, { until: number; remaining: number }>;
  };
}

export interface SummaryTotals {
  events: number;
  calls: number;
  denied: number;
  errors: number;
  tok_in_other: number;
  tok_in_cached: number;
  tok_out: number;
  tok_total: number;
  estimated: number;
  users: number;
  groups: number;
  avg_latency: number;
}

export interface SummaryData {
  range: { from: number; to: number };
  range_key: string;
  totals: SummaryTotals;
  trend: { day: string; tokens: number; calls: number; denied: number }[];
  top_scopes: { scope_type: string; scope_id: string; name?: string; tokens: number; events: number }[];
  by_model: { model: string; tokens: number }[];
  deny_reasons: { reason: string; cnt: number }[];
  /** bot 最后消息概况（M2）：最近回过话的会话数与类型分布 */
  bot?: { kinds: { kind: string; cnt: number }[]; sessions: number; latest_ts: number };
}

/** 「当前生效」的额度档位（与后端闸门的档位链严格一致）。 */
export interface EffectiveQuota {
  layer: string;
  layer_label: string;
  scope_type: string;
  scope_id: string;
  period: string;
  limit: number;
  used: number;
  mode: string;
  exceeded?: boolean;
}

/** 额度档位链：从具体到兜底，用于详情抽屉解释「为什么按这条额度算」。 */
export interface QuotaChainItem {
  layer: string;
  label: string;
  scope_type: string;
  scope_id: string;
  limits: Record<string, { limit_tokens: number; mode: string; reset_at: number | null }>;
  usage: Record<string, number>;
  effective: boolean;
  exceeded: boolean;
}

/** bot 在某个会话里的最后一条消息。 */
export interface LastBotMessage {
  scope_type: string;
  scope_id: string;
  platform_id: string;
  ts: number;
  kind: string;
  command: string;
  preview: string;
}

export interface FriendRow {
  platform_id: string;
  uin: string;
  nickname: string;
  remark: string;
  display_name: string;
  avatar: string;
  avatar_id: string;
  effect: "allow" | "deny" | "inherit";
  /** 对象级「指令权限」（feature=command）的显式值 */
  effect_command: "allow" | "deny" | "inherit";
  level_id: number | null;
  /** 私聊等级（原始配置；群聊场景的 level_id 已按「群聊专属 → 跟随私聊」解析） */
  level_id_base: number | null;
  /** 群聊专属等级（null = 跟随私聊） */
  level_id_group: number | null;
  level_name: string;
  quota: EffectiveQuota;
  quota_limit: number | null;
  quota_used: number | null;
  quota_mode: "enforce" | "observe" | null;
  quota_layer: string;
  today_tokens: number;
  last_bot_ts: number;
  last_bot_kind: string;
  last_bot_command: string;
  last_bot_preview: string;
  updated_at: number;
}

export interface GroupRow {
  platform_id: string;
  group_id: string;
  name: string;
  member_count: number;
  max_member_count: number;
  owner: string;
  avatar_id: string;
  effect: "allow" | "deny" | "inherit";
  /** 对象级「指令权限」（feature=command）的显式值 */
  effect_command: "allow" | "deny" | "inherit";
  level_id: number | null;
  level_name: string;
  quota: EffectiveQuota;
  quota_limit: number | null;
  quota_used: number | null;
  quota_mode: "enforce" | "observe" | null;
  quota_layer: string;
  today_tokens: number;
  /** 已缓存的成员数（0 = 还没同步过成员，群成员级管控不可用） */
  member_cached?: number;
  last_bot_ts: number;
  last_bot_kind: string;
  last_bot_command: string;
  last_bot_preview: string;
  updated_at: number;
}

export interface Paged<T> {
  total: number;
  rows: T[];
  page: number;
  size: number;
  sort?: string;
}

/** 一条限额规则。``used_tokens``：对象专属 → 该对象用量；模板（level/global）→ null。
 *
 *  ``scope_type='member'`` 时 ``scope_id`` 是「群号:QQ」（群成员维度，只影响该群）。 */
export interface QuotaRow {
  scope_type: "user" | "group" | "member" | "level" | "global";
  scope_id: string;
  period: "day" | "month" | "total";
  limit_tokens: number;
  used_tokens: number | null;
  mode: "enforce" | "observe";
  reset_at: number | null;
  updated_at: number;
}

/** 读额度规则；传 scopeId 只取这一个对象的（成员抽屉读初值用）。 */
export function apiGetQuota(scopeType: string, scopeId?: string) {
  const p = new URLSearchParams({ scope_type: scopeType });
  if (scopeId) p.set("scope_id", scopeId);
  return apiGet<{ items: QuotaRow[] }>(`/quota?${p.toString()}`);
}

/** 写额度规则。``limit_tokens=null`` = 删掉该周期（恢复继承）；``0`` = 明确「不限」。 */
export function apiSetQuota(
  items: {
    scope_type: string;
    scope_id: string;
    period: string;
    limit_tokens: number | null;
    mode?: string;
  }[],
) {
  return apiPost<{ applied: unknown[] }>("/quota", { items });
}

/** 清零用量（只动用量计数，不删额度配置）。 */
export function apiResetQuota(scopeType: string, scopeId: string, period?: string) {
  return apiPost<{ reset: number }>("/quota/reset", {
    scope_type: scopeType,
    scope_id: scopeId,
    period: period || undefined,
  });
}

/** 自定义等级（好友 / 群各一套）。 */
export interface LevelRow {
  id: number;
  kind: "user" | "group";
  name: string;
  description: string;
  effect: "inherit" | "allow" | "deny";
  /** 等级的默认**指令**权限（与 effect 分开配置） */
  effect_command: "inherit" | "allow" | "deny";
  /** 群聊场景下的默认 LLM 权限（只对 kind="user" 有意义；inherit = 跟随主值） */
  effect_group: "inherit" | "allow" | "deny";
  /** 群聊场景下的默认指令权限（同上） */
  command_effect_group: "inherit" | "allow" | "deny";
  sort_order: number;
  /** 模型路由：主提供商 id / 备用提供商 id（一项 = 一个「提供商 · 模型」） */
  provider_id: string;
  fallback_provider_id: string;
  /** 是否允许该等级的用户用 /切换模型 自助切换（false = 只读：能看不能切） */
  switch_enabled: boolean;
  /** 是否允许该等级的用户用 /切换模型检测（false = 不允许；会真打模型、消耗额度） */
  detect_enabled: boolean;
  /** 可切换的模型名单（空数组 = 展示兜底三项：当前 / 系统默认 / 备用） */
  switch_providers: string[];
  members: number;
  quotas: Record<string, { limit_tokens: number; mode: string; reset_at: number | null }>;
}

/** 等级编辑负载：``quotas`` 里 ``limit_tokens=null`` 或 ``delete=true`` 表示删除该周期。 */
export interface LevelPayload {
  id?: number;
  kind: "user" | "group";
  name: string;
  description?: string;
  effect?: "inherit" | "allow" | "deny";
  effect_command?: "inherit" | "allow" | "deny";
  effect_group?: "inherit" | "allow" | "deny";
  command_effect_group?: "inherit" | "allow" | "deny";
  sort_order?: number;
  provider_id?: string;
  fallback_provider_id?: string;
  /** 是否允许自助切换（缺省 = 沿用原值；默认关 = 只读） */
  switch_enabled?: boolean;
  /** 是否允许检测指令（缺省 = 沿用原值；默认关） */
  detect_enabled?: boolean;
  /** 可切换模型名单（缺省 = 沿用原值；传空数组 = 用兜底三项） */
  switch_providers?: string[];
  quotas?: { period: string; limit_tokens: number | null; mode?: string; delete?: boolean }[];
}

/** AstrBot 里已加载的对话模型提供商（一项 = 一个模型，对应一个提供商）。 */
export interface ProviderRow {
  id: string;
  /** 提供商（供应商）名 */
  name: string;
  /** 该提供商绑定的模型名 */
  model: string;
  /** 展示用：「名称 · 模型」 */
  label: string;
  type: string;
  modalities: string[];
  /** 是否是 AstrBot 当前默认的对话提供商 */
  is_default: boolean;
}

/** 某对象当前会走的模型（含来源等级与是否落到备用）。 */
export interface ModelRoute {
  layer?: string;
  label?: string;
  level_id?: number;
  provider_id?: string;
  fallback_provider_id?: string;
  used_fallback?: boolean;
  reason?: string;
  available?: string[];
  circuit_open?: string[];
}

export interface SubjectTotals {
  events: number;
  calls: number;
  denied: number;
  tok_in_other: number;
  tok_in_cached: number;
  tok_out: number;
  tok_total: number;
  estimated: number;
  avg_latency: number;
}

export interface SubjectDayPoint {
  day: string;
  tokens: number;
  calls: number;
  denied: number;
}

export interface SubjectDetail {
  type: "user" | "group";
  id: string;
  info: FriendRow | GroupRow | null;
  effect: "allow" | "deny" | "inherit";
  quotas: QuotaRow[];
  level_id: number | null;
  /** 好友的群聊专属等级（null = 跟随私聊；仅 type=user 时有意义） */
  level_id_group: number | null;
  level: LevelRow | null;
  level_quotas: QuotaRow[];
  quota_chain: QuotaChainItem[];
  quota: EffectiveQuota;
  model_route: ModelRoute;
  /** 专属模型（provider_id；空 = 跟随等级配置。好友仅私聊生效、群对全群生效） */
  subject_model?: string;
  /** 对象级指令权限：自己配的值 + 实际生效的层 */
  command_master: {
    effect: "allow" | "deny" | "inherit";
    resolved: "allow" | "deny";
    layer: string;
    layer_label: string;
    scope_type: string;
    scope_id: string;
  };
  usage: Record<string, { used_tokens: number; reset_at: number | null }>;
  bot: LastBotMessage | null;
  days: number;
  stats: {
    totals: SubjectTotals;
    series: SubjectDayPoint[];
    by_model: { model: string; tokens: number }[];
  };
  today: SubjectTotals;
  recent: Record<string, any>[];
}

/** 单个对象（好友/群）的详情：基础信息 + 权限 + 等级 + 额度档位链 + 区间用量与曲线。
 *
 *  ``scene`` 决定权限部分展示哪一套（好友分私聊 / 群聊两个维度）。 */
export function apiSubject(type: "user" | "group", id: string, days = 7, scene: "private" | "group" = "private") {
  return apiGet<SubjectDetail>(
    `/subject?type=${type}&id=${encodeURIComponent(id)}&days=${days}&scene=${scene}`,
  );
}

/** 手动同步好友 / 群列表（协议端往返，给足超时）。 */
export function apiSync() {
  return apiPost<{
    ok: boolean;
    total_friends: number;
    total_groups: number;
    platforms: { platform_id: string; ok: boolean; friends: number; groups: number; error?: string }[];
    error?: string;
  }>("/sync", {}, 60000);
}

/** 等级列表（含额度模板与成员数）。 */
/** 一条已注册的指令（AstrBot handler 注册表里带指令过滤器的处理器）。 */
export interface CommandRow {
  name: string;
  aliases: string[];
  desc: string;
  plugin: string;
  handler: string;
  is_group: boolean;
  /** 该指令的全局策略（inherit = 未配置，按「指令默认策略」走） */
  global_effect: "allow" | "deny" | "inherit";
  /** 例外规则（好友 / 群）；``scene`` 非空表示该规则只对某个场景生效 */
  rules: {
    scope_type: "user" | "group";
    scope_id: string;
    scene?: Scene;
    effect: "allow" | "deny" | "inherit";
  }[];
  rule_count: number;
}

export interface CommandsPayload {
  items: CommandRow[];
  default_effect: "allow" | "deny";
  enabled: boolean;
  priority: number;
  /** 规则里引用了但已经不存在（指令被卸载）的指令名 */
  stale: string[];
}

/** 指令清单 + 每条指令的权限规则。 */
export function apiCommands() {
  return apiGet<CommandsPayload>("/commands");
}

/** 清理已失效的指令规则（指令被卸载后残留）。 */
export function apiPruneCommands() {
  return apiPost<{ deleted: number; alive: number }>("/commands/prune", {});
}

/** 会话场景：好友这一层的规则可以只对私聊或只对群聊生效（'' = 两个场景都生效）。 */
export type Scene = "" | "private" | "group";

export interface PolicyItem {
  /** ``member`` 时 ``scope_id`` 是「群号:QQ」（群成员维度） */
  scope_type: "user" | "group" | "member" | "global";
  scope_id: string;
  effect: "allow" | "deny" | "inherit";
  feature: string;
  /** 只对 scope_type="user" 有意义；不传 = 通用规则 */
  scene?: Scene;
}

/** 批量写权限规则。``feature`` 三种取值：
 *  ``"llm"`` = LLM 对话权限；``"command"`` = 对象级指令总权限；
 *  ``"command:<指令名>"`` = 单条指令的规则。 */
export function apiSetPolicy(items: PolicyItem[]) {
  return apiPost<{ applied: unknown[] }>("/policy", { items });
}

/** 读取某 feature 的规则。 */
export function apiGetPolicy(feature: string, scopeType?: "user" | "group") {
  const qs = `feature=${encodeURIComponent(feature)}${scopeType ? `&scope_type=${scopeType}` : ""}`;
  return apiGet<{ items: Record<string, any>[] }>(`/policy?${qs}`);
}

/** 可用的对话模型提供商（等级里选「走哪个模型」）。 */
export function apiProviders() {
  return apiGet<{ items: ProviderRow[]; circuit_open: string[]; route_enabled: boolean }>("/providers");
}

export function apiLevels(kind?: "user" | "group") {
  return apiGet<{ items: LevelRow[]; counts: Record<string, number> }>(
    `/levels${kind ? `?kind=${kind}` : ""}`,
  );
}

export function apiSetLevel(body: LevelPayload) {
  return apiPost<{ id: number }>("/levels", body, 20000);
}

export function apiDeleteLevel(id: number) {
  return apiPost<{ deleted: number }>("/levels/delete", { id });
}

/** 批量设置等级（``level_id=null`` 表示取消归级）。
 *  好友（scope_type=user）分场景（v8）：scene=group 时写「群聊专属等级」，
 *  level_id=null 表示跟随私聊；scene=private（默认）写私聊等级。 */
export function apiSetSubjectLevel(
  items: {
    scope_type: "user" | "group";
    scope_id: string;
    level_id: number | null;
    scene?: "private" | "group";
  }[],
) {
  return apiPost<{ applied: unknown[] }>("/subject-level", { items });
}

/** 专属模型：好友（type=user，仅私聊生效）/ 群（type=group，对全群生效）。
 *  providerId 为空 = 恢复跟随等级配置。与 /切换模型 改的是同一份配置。 */
export function apiSetSubjectModel(
  scopeId: string,
  providerId: string,
  type: "user" | "group" = "user",
) {
  return apiPost<{ type: string; scope_id: string; provider_id: string }>("/subject/model", {
    type,
    scope_id: scopeId,
    provider_id: providerId,
  });
}

/** 批量取头像（返回 data URI 映射；只请求需要的 id，失败的不出现）。 */
export function apiAvatars(kind: "user" | "group", ids: string[], force = false) {
  const qs = `type=${kind}&ids=${encodeURIComponent(ids.join(","))}${force ? "&force=1" : ""}`;
  return apiGet<{ items: Record<string, string>; stats: Record<string, any>; got: number }>(
    `/avatars?${qs}`,
    45000,
  );
}

/** 强制更新头像缓存（``ids`` 省略 = 该类型全部已缓存的重取）。 */
export function apiRefreshAvatars(kind: "user" | "group", ids?: string[]) {
  return apiPost<{ refreshed: number; failed: number; stats: Record<string, any> }>(
    "/avatars/refresh",
    { type: kind, ids: ids && ids.length ? ids : undefined },
    120000,
  );
}

export interface ConfigPayload {
  items: Record<string, any>;
  schema: Record<string, any>;
}

// ---------------------------------------------------------------- 统计

/** 一条用量 / 事件流水（LLM 调用、被拒、指令触发与被拦都走这张表）。 */
export interface UsageRow {
  id: number;
  ts: number;
  kind: "llm" | "command" | string;
  status: "ok" | "denied" | "error" | string;
  deny_reason: string;
  command_name: string;
  scope_type: string;
  scope_id: string;
  /** 对象的展示名（备注 / 昵称 / 群名；查不到为空，前端回退显示 id） */
  scope_name?: string;
  sender_id: string;
  group_id: string;
  provider_id: string;
  model: string;
  tok_in_other: number;
  tok_in_cached: number;
  tok_out: number;
  estimated: number;
  latency_ms: number;
}

/** 明细查询条件（也用于导出，保证「看到的」和「导出的」一致）。 */
export interface UsageFilter {
  range?: "1d" | "7d" | "30d";
  kind?: string;
  status?: string;
  scope_type?: string;
  scope_id?: string;
  sender_id?: string;
  page?: number;
  size?: number;
}

function usageQuery(f: UsageFilter): string {
  const p = new URLSearchParams();
  p.set("range", f.range || "7d");
  for (const k of ["kind", "status", "scope_type", "scope_id", "sender_id"] as const) {
    const v = f[k];
    if (v) p.set(k, String(v));
  }
  p.set("page", String(f.page || 1));
  p.set("size", String(f.size || 50));
  return p.toString();
}

/** 用量明细（分页）。 */
export function apiUsage(f: UsageFilter = {}) {
  return apiGet<{ total: number; rows: UsageRow[]; page: number; size: number }>(
    `/usage?${usageQuery(f)}`,
  );
}

/** 导出用量明细为 CSV。后端返回文本内容，由前端用 Blob 落地（桥接下无法直接下载 URL）。 */
export function apiExportUsage(f: UsageFilter = {}) {
  return apiGet<{ filename: string; content: string }>(`/usage/export?${usageQuery(f)}`, 60000);
}

/** 指令维度统计：最常触发 / 最常被拦 Top 榜 + 24h×7d 时段热力图。 */
export interface CommandStats {
  range: { from: number; to: number };
  top_ok: { command: string; cnt: number }[];
  top_denied: { command: string; cnt: number }[];
  heatmap: { dow: number; hour: number; cnt: number }[];
  /** 「记录指令触发流水」开关，关闭时 Top 榜只有被拦记录 */
  track_enabled: boolean;
}

export function apiCommandStats(range: "1d" | "7d" | "30d" = "7d", limit = 10) {
  return apiGet<CommandStats>(`/stats/commands?range=${range}&limit=${limit}`);
}

// ---------------------------------------------------------------- 群成员

/** 一个群成员（缓存自协议端）+ 他的权限与本群今日用量。 */
export interface GroupMemberRow {
  platform_id: string;
  group_id: string;
  user_id: string;
  nickname: string;
  card: string;
  /** owner | admin | member */
  role: string;
  level: string;
  joined_at: number;
  updated_at: number;
  /** 展示名：群名片优先，其次昵称 */
  display_name: string;
  avatar_id: string;
  /** 权限规则的 scope_id（`群号:QQ`），写规则时用它 */
  scope_id: string;
  effect: "allow" | "deny" | "inherit";
  effect_command: "allow" | "deny" | "inherit";
  /** 该成员在**这个群**里的今日 token 用量 */
  today_tokens: number;
  /** 生效额度档位（命中哪一层就展示哪一层；`layer` 为空 = 不限量） */
  quota: EffectiveQuota;
  quota_limit: number | null;
  quota_used: number | null;
  quota_mode: "enforce" | "observe" | null;
  quota_layer: string;
}

/** 某个群的成员列表（读本地缓存，不打协议端）。 */
export function apiGroupMembers(groupId: string, q = "", page = 1, size = 200) {
  const p = new URLSearchParams({ group_id: groupId, page: String(page), size: String(size) });
  if (q) p.set("q", q);
  return apiGet<{
    total: number;
    rows: GroupMemberRow[];
    page: number;
    size: number;
    group_id: string;
    synced_at: number;
  }>(`/group/members?${p.toString()}`);
}

/** 从协议端同步某个群的成员列表（较慢，需要协议端支持 get_group_member_list）。 */
export function apiSyncGroupMembers(groupId: string, platformId = "") {
  return apiPost<{ ok: boolean; count: number; group_id: string; platform_id: string; error?: string }>(
    "/group/members/sync",
    { group_id: groupId, platform_id: platformId },
    60000,
  );
}

// ---------------------------------------------------------------- 指令 × 对象矩阵

/** 矩阵里的一行：某条指令对「这个对象」的结论（后端 /commands/matrix 的行结构，
 * 与 /commands 的 CommandRow 不同：没有 global_effect / rules 等字段，别混用）。 */
export interface CommandMatrixRow {
  name: string;
  /** 与 name 同值（后端保留两个字段是为了语义清晰） */
  command: string;
  desc: string;
  plugin: string;
  aliases: string[];
  is_group: boolean;
  /** 最终结论：能不能用 */
  allow: boolean;
  /** 结论来自哪一层（空 = 系统默认） */
  layer: string;
  layer_label: string;
  /** 该对象在这条指令上的**显式**规则（``inherit`` = 没单独配） */
  explicit: "allow" | "deny" | "inherit";
}

export interface CommandMatrix {
  scope_type: "user" | "group" | "member" | "level" | "global";
  scope_id: string;
  scene: "private" | "group";
  /** 等级的类型（仅 scope_type=level 时非空） */
  level_kind: string;
  /** 这一层能不能配单条指令规则（等级层不能，前端据此隐藏编辑入口） */
  editable: boolean;
  summary: {
    total: number;
    allowed: number;
    denied: number;
    /** 按「结论来自哪一层」统计 */
    by_layer: Record<string, number>;
  };
  rows: CommandMatrixRow[];
}

/** 指令 × 对象矩阵：某对象能用哪些指令、结论来自哪一层。 */
export function apiCommandMatrix(
  scopeType: string,
  scopeId: string,
  scene: "private" | "group" = "private",
) {
  const p = new URLSearchParams({ scope_type: scopeType, scope_id: scopeId, scene });
  return apiGet<CommandMatrix>(`/commands/matrix?${p.toString()}`, 60000);
}

// ---------------------------------------------------------------- 规则备份 / 迁移

/** 导出全部**规则类**配置（权限 / 等级 / 归级 / 额度）为 JSON 文本。 */
export function apiExportRules() {
  return apiGet<{ filename: string; content: string; meta: Record<string, any> }>(
    "/rules/export",
    60000,
  );
}

/** 导入统计。 */
export interface ImportStats {
  mode: "merge" | "replace";
  levels: number;
  subject_levels: number;
  quotas: number;
  policies: number;
  /** 被跳过的不合法行数（导入是「尽量恢复」，坏行不会让整次失败） */
  skipped: number;
}

/** 导入规则。``replace`` 会先清空「权限 / 等级 / 归级 / 额度」再导入（用量与日志不动）。 */
export function apiImportRules(mode: "merge" | "replace", content: string) {
  return apiPost<ImportStats>("/rules/import", { mode, content }, 60000);
}

/** 管理员操作审计（谁在什么时候改了什么规则）。 */
export interface AuditRow {
  id: number;
  ts: number;
  actor: string;
  action: string;
  payload: string;
}

export function apiAudit(page = 1, size = 50) {
  return apiGet<{ total: number; rows: AuditRow[]; page: number; size: number }>(
    `/audit?page=${page}&size=${size}`,
  );
}
