"""
astrbot_plugin_dual_persona — 双人格热切换插件
==============================================
一体双生，共用记忆。直接复用 AstrBot 已配置的「人格」(Persona)，
在两个人设之间一键切换，共享全部聊天上下文。

v3.1.0 新增：
- 面板分二级：主控（只读总览）+ 设置（可直接在面板里改配置并保存）

v3.0.1 修复：
- 衰减永不触发（sweep 每次重置时间戳，凑不满 10 分钟）→ 切到人格 B 后会一直下不来
- 无人说话时人格不回退 → 加每 5 分钟闲置扫描
- 单字情绪词（烦/累/闷）被忽略 → allow_single_char_words 开关，默认允许

v3.0.0 主要变化：
- 情绪数值化：弱词不再「命中即切」，改为累积 satiety（0-100），满格才激发出另一面
- 最短驻留：切过去后至少待 N 分钟，期间不衰减；驻留内叫强词会重置计时
- 时间衰减：每条消息 -1，每 10 分钟再 -10，掉到 release 阈值以下自动回退

v2.6.0 主要变化：
- 切换人格后可用主动消息发一条「现在为 xx」提示，走 context.send_message，
  不进入对话历史、不影响 LLM 上下文（可配 off/always/auto_only/manual_only）

v2.5.0 主要变化：
- 关键词判定重做：强词（命中即切）+ 弱词计分（需达阈值）+ 否定/疑问排除 + 防抖冷却

v2.4.0 主要变化：
- 关键词映射改为交叉语义：trigger_words_a 命中 -> 人格 B，trigger_words_b 命中 -> 人格 A
- 新增「模型自主切换」：模型自己判断该换人时，在回复末尾输出 [[switch:a]] / [[switch:b]]，
  插件检测后剥离标记并切换人格（llm_switch_enabled / llm_switch_rules）

v2.3.0 主要变化：
- 新增 prompt_mode：prepend（人格在前，默认）/ append（平台规则在前）/ override（只用人格）
- 新增 /人格预览：查看本轮实际注入的完整 system prompt，用来核对人设到底进没进去
- 触发词默认清空，避免日常情绪词误唤醒里人格

v2.2.0 变化：
- 新增 state_scope：global（群聊与私聊共用同一个当前人格，默认）/ session（按会话隔离）
- 新增 auto_switch_admin_only：关键词自动切换只对管理员消息生效（默认开）

v2.1.0 变化：
- 会话隔离：人格状态按 unified_msg_origin 分开保存（群里切换不再串到私聊）
- 管理员权限真正生效：读插件配置 admin_uids，兜底读全局 admins_id
- 深夜模式不再压制手动切换，且每个夜间区间只自动切一次、不再每条消息写库
- 快捷指令 /<人格名>来了 改用显示名称匹配，并读 AstrBot 的 wake_prefix
- 人格缓存 60 秒 TTL，改完人设不用重启
- 新增「AI 自知」：把当前人格与生效设置注入 system prompt
- 新增 WebUI 状态面板：views/status（需 AstrBot 支持插件可视化视图）
"""

import asyncio
import re
import time
from datetime import datetime, timedelta

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_dual_persona"
# 否定标记：命中词前 4 字内出现这些，视为否定，不触发
_NEG_MARKERS = ("一点都不", "才不", "哪有", "不太", "并不", "不", "没", "别")
# 疑问/玩笑尾巴：弱词命中时整句以此结尾且含疑问词，则忽略
_QUESTION_TAILS = ("吗", "吧", "呢", "么")

# 模型自主切换标记：[[switch:a]] / [[switch:b]]
_SWITCH_MARKER = re.compile(r"\[\[\s*switch\s*[:：]\s*([abAB])\s*\]\]", re.I)
PLUGIN_VERSION = "3.1.3"

try:  # astrbot.api.web 只在较新版本提供，缺失时只影响状态面板
    from astrbot.api.web import error_response, json_response, request  # noqa: F401
    _WEB_API_AVAILABLE = True
except Exception:  # pragma: no cover
    error_response = None
    json_response = None
    request = None
    _WEB_API_AVAILABLE = False


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：中日韩字符按 1 个 ≈ 0.75 token，其余按 4 字符 ≈ 1 token"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff")
    other = len(text) - cjk
    return int(cjk * 0.75 + other / 4)


class DualPersona(Star):
    """双人格热切换插件 — 读取 AstrBot 内置人设"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)

        cfg = config if config else {}
        # 保留原始配置对象（AstrBotConfig 支持 save_config()），供面板保存设置
        self._raw_config = config

        # 用户通过 WebUI（_special: select_persona）选择的两个人格的 ID
        self._persona_a_id: str = cfg.get("persona_a_id", "") or ""
        self._persona_b_id: str = cfg.get("persona_b_id", "") or ""

        # 情绪触发词：trigger_words_a 命中 -> 切到人格 A；trigger_words_b 命中 -> 切到人格 B
        self._trigger_words_a: list = cfg.get("trigger_words_a", []) or []
        self._trigger_words_b: list = cfg.get("trigger_words_b", []) or []

        # 深夜模式时间配置
        self._night_start_time: str = cfg.get("night_start_time", "23:00") or "23:00"
        self._night_end_time: str = cfg.get("night_end_time", "04:00") or "04:00"
        # 深夜时段倾向的人格
        self._night_target: str = (cfg.get("night_target", "a") or "a").strip().lower()
        if self._night_target not in ("a", "b"):
            self._night_target = "a"

        self._auto_switch_enabled: bool = bool(cfg.get("auto_switch_enabled", True))
        self._manual_switch_enabled: bool = bool(cfg.get("manual_switch_enabled", True))

        # 人格状态范围：global（群聊 / 私聊共用一个当前人格）/ session（按会话隔离）
        self._state_scope: str = (cfg.get("state_scope", "global") or "global").strip()
        if self._state_scope not in ("global", "session"):
            self._state_scope = "global"

        # 关键词自动切换是否仅管理员消息生效
        self._auto_switch_admin_only: bool = bool(cfg.get("auto_switch_admin_only", True))

        # 强词：命中立即切换，不受冷却限制
        self._strong_words_to_b: list = cfg.get(
            "strong_words_to_b", ["姐姐", "姐姐出来"]
        ) or []
        self._strong_words_to_a: list = cfg.get(
            "strong_words_to_a", ["妹妹", "换回来"]
        ) or []
        # 弱词：需要命中 weak_need 个才切换
        try:
            self._weak_need: int = max(1, int(cfg.get("weak_need", 2) or 2))
        except Exception:
            self._weak_need = 2
        try:
            self._switch_cooldown: float = (
                max(0.0, float(cfg.get("switch_cooldown_min", 10) or 0)) * 60
            )
        except Exception:
            self._switch_cooldown = 600.0

        # ---- 情绪数值（satiety 0-100）：刺激累积，满格才激发出另一面 ----
        self._satiety_enabled: bool = bool(cfg.get("satiety_enabled", True))

        def _num(key: str, default: float, lo: float = 0.0, hi: float = 1000.0) -> float:
            try:
                return max(lo, min(hi, float(cfg.get(key, default) or default)))
            except Exception:
                return float(default)

        self._sat_threshold: float = _num("satiety_threshold", 100, 10, 100)
        self._sat_weak_add: float = _num("satiety_weak_add", 25, 1, 100)
        self._sat_mood_add: float = _num("satiety_mood_add", 5, 0, 100)
        # 是否允许单字情绪词（烦 / 累 / 闷）参与判定
        self._allow_single_char: bool = bool(cfg.get("allow_single_char_words", True))
        self._sat_decay_msg: float = _num("satiety_decay_msg", 1, 0, 50)
        self._sat_decay_10min: float = _num("satiety_decay_10min", 10, 0, 100)
        self._sat_release: float = _num("satiety_release", 20, 0, 100)
        self._min_dwell: float = _num("min_dwell_min", 20, 0, 720) * 60
        self._sat_show: bool = bool(cfg.get("satiety_show_in_prompt", False))

        # 切换提示：off / always / auto_only / manual_only
        self._switch_notice_mode: str = (
            cfg.get("switch_notice_mode", "auto_only") or "auto_only"
        ).strip()
        if self._switch_notice_mode not in ("off", "always", "auto_only", "manual_only"):
            self._switch_notice_mode = "auto_only"
        self._switch_notice_template: str = (
            cfg.get("switch_notice_template", "— 现在为「{name}」（人格 {key}）")
            or "— 现在为「{name}」（人格 {key}）"
        ).strip()

        # 模型自主切换：让模型自己判断该换人，用 [[switch:a]] / [[switch:b]] 标记
        self._llm_switch_enabled: bool = bool(cfg.get("llm_switch_enabled", False))
        self._llm_switch_rules: str = (cfg.get("llm_switch_rules", "") or "").strip()

        # 切换台词模式：none / default / personalized / custom
        self._switch_lines_mode: str = cfg.get("switch_lines_mode", "default") or "default"
        self._switch_lines: dict = cfg.get("switch_lines", {}) or {}
        self._natural_handoff: bool = bool(cfg.get("natural_handoff", True))

        # 人设与平台提示词的拼装方式：prepend / append / override
        self._prompt_mode: str = (cfg.get("prompt_mode", "prepend") or "prepend").strip()
        if self._prompt_mode not in ("prepend", "append", "override"):
            self._prompt_mode = "prepend"
        self._switch_trigger_suffix: str = cfg.get("switch_trigger_suffix", "来了") or "来了"

        # AI 自知：把当前人格与生效设置注入 system prompt
        self._self_aware: bool = bool(cfg.get("self_aware", True))
        self._self_aware_triggers: bool = bool(cfg.get("self_aware_include_triggers", False))
        self._self_aware_compact: bool = bool(cfg.get("self_aware_compact", True))

        # 管理员 UID（读插件配置，兼容字符串与列表两种写法）
        raw_admin = cfg.get("admin_uids", "") or ""
        if isinstance(raw_admin, str):
            self._admin_uids = {u.strip() for u in raw_admin.replace("，", ",").split(",") if u.strip()}
        else:
            self._admin_uids = {str(u).strip() for u in raw_admin if str(u).strip()}

        # 会话状态：unified_msg_origin -> {key, manual, handoff_key, night_tag, updated}
        self._sessions: dict[str, dict] = {}
        # 人格缓存：'a'/'b' -> (monotonic 时间戳, data)
        self._persona_cache: dict[str, tuple[float, dict]] = {}
        self._persona_ttl: float = 60.0

        self._night_timer_task = None

        if not self._persona_a_id and not self._persona_b_id:
            logger.warning(
                "[DualPersona] 尚未在 WebUI 插件配置中选择人格，"
                "请前往 设置 → 插件配置 → 双人格热切换 选择两个人设。"
            )

        # 状态面板后端（老版本 AstrBot 没有 register_web_api，静默跳过）
        self._register_web_api()

    # ==================================================================
    # 会话状态
    # ==================================================================
    def _state_key(self, event: AstrMessageEvent) -> str:
        """global 模式下所有会话共用一份状态；session 模式下按会话隔离"""
        if self._state_scope == "session":
            return event.unified_msg_origin
        return "global"

    async def _state(self, event: AstrMessageEvent) -> dict:
        """取（或惰性创建）当前作用域的人格状态"""
        key = self._state_key(event)
        st = self._sessions.get(key)
        if st is None:
            saved = await self.get_kv_data(f"persona:{key}", default="a")
            saved = saved if saved in ("a", "b") else "a"
            st = {
                "key": saved,
                "manual": False,      # 用户手动切换过 -> 深夜模式不再自动压制
                "handoff_key": saved,
                "night_tag": None,
                "last_auto_ts": 0.0,
                "satiety": 0.0,
                "sat_ts": time.time(),
                "dwell_until": 0.0,
                "updated": time.time(),
            }
            if len(self._sessions) >= 1000:      # 防止无限增长
                self._sessions.clear()
            self._sessions[key] = st
        # 自愈：manual 标记只对「锁定在 B」有意义。
        # 旧版本可能留下 manual=True 但 key=A 的脏状态，会把深夜规则永久挡住。
        if st.get("manual") and st.get("key") != "b":
            st["manual"] = False
            logger.debug(
                f"[DualPersona] 自愈：{key} 的 manual 指向人格 A，已解除锁定"
            )
        st["updated"] = time.time()
        return st

    def _spawn_notice(self, event: AstrMessageEvent, key: str) -> None:
        """后台发送切换提示，不阻塞本轮生成"""
        try:
            task = asyncio.create_task(self._send_switch_notice(event, key))
        except RuntimeError:
            return
        tasks = getattr(self, "_bg_tasks", None)
        if tasks is None:
            tasks = self._bg_tasks = set()
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _send_switch_notice(self, event: AstrMessageEvent, key: str) -> None:
        """用主动消息发一条状态提示：不进入对话历史，不污染上下文"""
        mode = self._switch_notice_mode
        if mode == "off":
            return
        name = self._get_persona_name(key)
        try:
            text = (
                self._switch_notice_template
                .replace("{name}", name)
                .replace("{key}", key.upper())
            )
        except Exception:
            text = f"— 现在为「{name}」"
        try:
            await self.context.send_message(
                event.unified_msg_origin, MessageChain().message(text)
            )
        except Exception:
            logger.debug("[DualPersona] 发送切换提示失败（平台可能不支持主动消息）", exc_info=True)

    async def _do_switch(
        self, event: AstrMessageEvent, st: dict, key: str, *, manual: bool
    ) -> None:
        """执行人格切换并持久化（只在 key 真的变化时写库）"""
        changed = st["key"] != key
        st["key"] = key
        if manual:
            # 手动切到 A = 主动放弃锁定；手动切到 B = 锁定 B
            st["manual"] = key == "b"
        else:
            st["last_auto_ts"] = time.time()

        # 进 B：补满情绪值并开始驻留（否则会立刻被判为「已回落」）
        # 回 A：清空数值与驻留
        if self._satiety_enabled:
            if key == "b":
                st["satiety"] = float(self._sat_threshold)
                st["dwell_until"] = (
                    time.time() + self._min_dwell if self._min_dwell else 0.0
                )
            else:
                st["satiety"] = 0.0
                st["dwell_until"] = 0.0
            st["sat_ts"] = time.time()
        if changed:
            mode = self._switch_notice_mode
            if (
                mode == "always"
                or (mode == "auto_only" and not manual)
                or (mode == "manual_only" and manual)
            ):
                self._spawn_notice(event, key)
            scope_key = self._state_key(event)
            await self.put_kv_data(f"persona:{scope_key}", key)
            logger.info(
                f"[DualPersona] {scope_key} -> "
                f"{self._get_persona_name(key)} (key={key}, manual={manual})"
            )

    # ==================================================================
    # LLM 请求钩子 — 注入人格 prompt + 自知状态块
    # ==================================================================
    @filter.on_llm_request()
    async def inject_persona(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """每次 LLM 请求前改写 system_prompt。不改动上下文，历史完整保留。"""
        _t0 = time.perf_counter()
        st = await self._state(event)

        if self._auto_switch_enabled:
            target = await self._auto_switch(event, st)
            if target and target != st["key"]:
                await self._do_switch(event, st, target, manual=False)

        data = await self._get_persona_data(st["key"])
        prompt = (data or {}).get("prompt", "")
        if not prompt:
            now = time.time()
            if now - getattr(self, "_last_warn_ts", 0) > 300:   # 5 分钟最多提醒一次
                self._last_warn_ts = now
                logger.warning(
                    f"[DualPersona] 当前人格（key={st['key']}）读不到内容，本轮未注入任何人设。"
                    f"请检查插件配置中选择的人格是否仍然存在。"
                )
            return
        name = (data or {}).get("name", "") or st["key"].upper()

        req.system_prompt = self._build_system_prompt(st, name, prompt, req.system_prompt)

        # 自然接管：切换后的第一次请求注入一次性提示
        if self._natural_handoff and st["key"] != st["handoff_key"]:
            self._inject_handoff_prompt(req)
            st["handoff_key"] = st["key"]

        # token 账本：人设 / 状态块 / 最终 system_prompt
        persona_tokens = estimate_tokens(prompt)
        state_tokens = estimate_tokens(self._state_block(st)) if self._self_aware else 0
        total_tokens = estimate_tokens(req.system_prompt)
        st["metrics"] = {
            "persona_tokens": persona_tokens,
            "state_tokens": state_tokens,
            "total_tokens": total_tokens,
            "updated": time.time(),
        }
        logger.debug(
            f"[DualPersona] 注入人格 {name} | session={event.unified_msg_origin} | "
            f"约 {total_tokens} tokens（人设 {persona_tokens} + 状态 {state_tokens}）"
        )

    def _build_system_prompt(
        self, st: dict, name: str, prompt: str, original: str | None
    ) -> str:
        """按 prompt_mode 拼装最终的 system_prompt"""
        persona_block = f"# 当前人格：{name}\n{prompt}"
        original = (original or "").strip()

        if self._prompt_mode == "override" or not original:
            blocks = [persona_block]
        elif self._prompt_mode == "append":
            blocks = [original, persona_block]
        else:  # prepend：人格在前，平台规则在后
            blocks = [persona_block, original]

        if self._self_aware:
            blocks.append(self._state_block(st))
        return "\n\n---\n\n".join(blocks)

    def _state_block(self, st: dict) -> str:
        """AI 自知：让模型知道自己当前是谁（默认压缩成一行，省 token）"""
        if self._self_aware_compact:
            key = st["key"]
            cur = self._get_persona_name(key)
            other = self._get_persona_name("b" if key == "a" else "a")
            parts = [f"你是{cur}（{key.upper()}）", f"另一面{other}"]
            if self._auto_switch_enabled:
                parts.append(f"深夜倾向{self._get_persona_name(self._night_target)}")
            if self._state_scope == "global":
                parts.append("人格全局生效")
            if self._sat_show:
                parts.append(f"情绪 {int(st.get('satiety', 0))}/{int(self._sat_threshold)}")
            line = "# 运行状态：" + "｜".join(parts) + "（不要朗读本行）"
            if self._llm_switch_enabled:
                if self._llm_switch_rules:
                    line += f"\n换人规则：{self._llm_switch_rules}"
                line += "\n需要换人时在末尾单独一行输出 [[switch:a]] 或 [[switch:b]]"
            return line

        key = st["key"]
        cur = self._get_persona_name(key)
        other_key = "b" if key == "a" else "a"
        other = self._get_persona_name(other_key)
        lines = [
            "# 运行状态（供你自我认知，不要在回复中朗读本段）",
            f"- 你现在是：{cur}（人格 {'A' if key == 'a' else 'B'}）",
            f"- 另一个人格：{other}（人格 {'B' if key == 'a' else 'A'}），与你共用同一段对话记忆",
        ]
        if self._auto_switch_enabled:
            night_name = self._get_persona_name(self._night_target)
            lines.append(
                f"- 自动切换：开（深夜 {self._night_start_time}–{self._night_end_time} 倾向 {night_name}）"
            )
        else:
            lines.append("- 自动切换：关")
        if self._manual_switch_enabled:
            lines.append(
                f"- 用户可用 /切换人格 或 /<人格名>{self._switch_trigger_suffix} 手动切换"
            )
        if self._self_aware_triggers:
            a_words = "、".join([w for w in self._trigger_words_a if w][:8]) or "无"
            b_words = "、".join([w for w in self._trigger_words_b if w][:8]) or "无"
            lines.append(f"- 切到人格 A 的触发词：{a_words}")
            lines.append(f"- 切到人格 B 的触发词：{b_words}")
        if self._state_scope == "global":
            lines.append("- 当前人格对私聊和所有群聊统一生效（不是每个会话各自独立）")
        if self._auto_switch_enabled and self._auto_switch_admin_only:
            lines.append("- 关键词自动切换只对管理员的消息生效，普通用户发关键词不会换人格")
        lines.append(
            "- 若用户问「你现在是谁 / 当前人格 / 当前设置」，按以上如实简短回答；"
            "没被问就不要主动提这些。"
        )
        if self._llm_switch_enabled:
            a_name = self._get_persona_name("a")
            b_name = self._get_persona_name("b")
            if self._llm_switch_rules:
                lines.append(f"- 换人判断规则：{self._llm_switch_rules}")
            lines.append(
                "- 你可以在这一轮结束时决定换人：在回复最后单独一行输出 "
                f"[[switch:a]]（换成{a_name}）或 [[switch:b]]（换成{b_name}）。"
            )
            lines.append(
                "- 只有真的该换时才输出，一轮最多一次；这一行会被系统删除，"
                "不要解释、不要写进正文、不要出现在说话内容里"
            )
        return "\n".join(lines)

    @filter.on_llm_response()
    async def detect_switch_marker(self, event: AstrMessageEvent, resp) -> None:
        """模型自主切换：检测回复里的 [[switch:x]]，剥离标记并切换人格"""
        if not self._llm_switch_enabled or resp is None:
            return
        try:
            text = resp.completion_text or ""
        except Exception:
            return
        match = _SWITCH_MARKER.search(text)
        if not match:
            return
        try:
            resp.completion_text = _SWITCH_MARKER.sub("", text).strip()
        except Exception:
            pass
        target = match.group(1).lower()
        st = await self._state(event)
        if target == st["key"]:
            return
        await self._do_switch(event, st, target, manual=False)
        logger.info(
            f"[DualPersona] 模型自主切换 -> {self._get_persona_name(target)}"
            f" | session={event.unified_msg_origin}"
        )

    def _inject_handoff_prompt(self, req: ProviderRequest) -> None:
        """注入一次性自然接管提示"""
        try:
            from astrbot.core.agent.message import TextPart

            handoff_text = (
                "【切换提示，仅本轮有效】\n"
                "你刚刚接管了这个会话。当前对话的上下文和历史记忆仍然有效。\n"
                "请以你的人格风格，自然、流畅地回应上面的消息。\n"
                "绝对不要说你刚切换过来、刚接管、刚出现等 meta 话语。"
            )
            req.extra_user_content_parts.append(TextPart(text=handoff_text))
        except Exception as e:
            logger.debug(f"[DualPersona] 注入自然接管提示失败: {e}")

    # ==================================================================
    # 自动切换
    # ==================================================================
    @staticmethod
    def _is_negated(msg: str, pos: int) -> bool:
        prefix = msg[max(0, pos - 4):pos]
        return any(m in prefix for m in _NEG_MARKERS)

    @staticmethod
    def _is_question(msg: str) -> bool:
        tail = msg.rstrip()
        return tail.endswith(("？", "?")) and any(k in tail for k in _QUESTION_TAILS)

    def _hit_words(self, msg: str, words, *, strong: bool) -> list:
        """返回命中的词；强词只做否定排除，弱词额外排除疑问句"""
        hits = []
        for raw in words or []:
            word = str(raw or "").strip()
            if not word:
                continue
            if len(word) < 2 and not self._allow_single_char:
                # 单字词误伤大（「不乖」「乖吗」都会命中），默认跳过
                continue
            start = 0
            while True:
                pos = msg.find(word, start)
                if pos < 0:
                    break
                start = pos + len(word)
                if self._is_negated(msg, pos):
                    continue
                if not strong and self._is_question(msg):
                    continue
                hits.append(word)
                break
        return hits

    def _keyword_allowed(self, event: AstrMessageEvent, msg: str) -> bool:
        if self._auto_switch_admin_only and not self._is_admin(event):
            logger.debug(
                f"[DualPersona] 命中关键词但发送者非管理员，跳过: {msg[:40]}"
            )
            return False
        return True

    # ------------------------------------------------------------------
    # 情绪数值 satiety：累积 → 越阈值 → 激发另一面 → 驻留 → 衰减回落
    # ------------------------------------------------------------------
    def _decay_satiety(self, st: dict) -> float:
        """惰性衰减：按距上次计算的时间与消息自然回落"""
        now = time.time()
        last = float(st.get("sat_ts") or now)
        value = float(st.get("satiety") or 0.0)
        # 驻留期内不衰减
        if st.get("dwell_until") and now < float(st["dwell_until"]):
            st["sat_ts"] = now
            st["satiety"] = value
            return value
        minutes = (now - last) / 60.0
        if minutes > 0 and self._sat_decay_10min > 0:
            drops = int(minutes // 10)
            if drops > 0:
                value -= drops * self._sat_decay_10min
                # 只有真的衰减了才推进时间戳，否则零碎调用会永远凑不满 10 分钟
                st["sat_ts"] = now
        st["satiety"] = max(0.0, value)
        return st["satiety"]

    def _add_satiety(self, st: dict, delta: float, reason: str = "") -> float:
        if delta <= 0:
            return float(st.get("satiety") or 0.0)
        self._decay_satiety(st)
        st["satiety"] = min(float(self._sat_threshold), float(st.get("satiety") or 0.0) + delta)
        logger.debug(
            f"[DualPersona] satiety +{delta:g} -> {st['satiety']:g}"
            f"/{self._sat_threshold:g} {reason}"
        )
        return st["satiety"]

    def _reset_satiety(self, st: dict) -> None:
        st["satiety"] = 0.0
        st["sat_ts"] = time.time()

    def _in_dwell(self, st: dict) -> bool:
        until = float(st.get("dwell_until") or 0)
        return until > 0 and time.time() < until

    def _in_cooldown(self, st: dict) -> bool:
        if self._switch_cooldown <= 0:
            return False
        last = float(st.get("last_auto_ts") or 0)
        return last > 0 and (time.time() - last) < self._switch_cooldown

    async def _auto_switch(self, event: AstrMessageEvent, st: dict) -> str | None:
        msg = event.message_str or ""
        allowed = self._keyword_allowed(event, msg)

        # ---- 1) 强词：显式叫人，直接切（不受数值限制）----
        if self._hit_words(msg, self._strong_words_to_b, strong=True):
            if allowed:
                logger.debug("[DualPersona] 强词触发 -> 人格 B（记为手动，重置驻留）")
                # 强词是用户显式点名：等同于手动选择，不被数值回落赶走
                st["manual"] = True
                st["dwell_until"] = time.time() + self._min_dwell if self._min_dwell else 0.0
                st["satiety"] = float(self._sat_threshold)
                st["sat_ts"] = time.time()
                return "b"
        elif self._hit_words(msg, self._strong_words_to_a, strong=True):
            if allowed:
                logger.debug("[DualPersona] 强词触发 -> 人格 A（解除锁定）")
                self._reset_satiety(st)
                st["dwell_until"] = 0.0
                st["manual"] = False        # 显式叫回 A = 解除锁定，深夜恢复生效
                return "a"
        else:
            # ---- 2) 弱词：只加分，不直接切 ----
            hits_to_b = self._hit_words(msg, self._trigger_words_a, strong=False)
            hits_to_a = self._hit_words(msg, self._trigger_words_b, strong=False)
            weak_target = None
            hit_count = 0
            if hits_to_b:
                weak_target, hit_count = "b", len(hits_to_b)
            elif hits_to_a:
                weak_target, hit_count = "a", len(hits_to_a)

            if weak_target and allowed:
                gained = hit_count * self._sat_weak_add
                # 数值只衡量「离 B 被激发还有多远」：
                # 指向 B 的词加分；指向 A 的安抚词降分；已处于 B 时只做维持
                if weak_target == "b":
                    if st["key"] == "b":
                        self._add_satiety(st, self._sat_mood_add, "(维持)")
                    else:
                        self._add_satiety(
                            st, gained, f"(弱词 {hit_count} 个 → B)"
                        )
                elif weak_target == "a":
                    cur = float(st.get("satiety") or 0.0)
                    st["satiety"] = max(0.0, cur - gained)
                    st["sat_ts"] = time.time()
                    logger.debug(
                        f"[DualPersona] satiety -{gained:g} -> {st['satiety']:g}（安抚词 → A）"
                    )

            # ---- 3) 越阈值：激发另一面 ----
            if self._satiety_enabled:
                self._decay_satiety(st)
                value = float(st.get("satiety") or 0)
                if value >= self._sat_threshold and st["key"] != "b":
                    st["dwell_until"] = time.time() + self._min_dwell if self._min_dwell else 0.0
                    st["satiety"] = float(self._sat_threshold)
                    st["sat_ts"] = time.time()
                    logger.info(
                        f"[DualPersona] 情绪累积达 {value:g}/{self._sat_threshold:g} "
                        f"→ 激发人格 B"
                    )
                    return "b"
                # ---- 4) 回落：驻留期满且数值低（手动选择的不受数值回落影响）----
                if (
                    st["key"] == "b"
                    and not st.get("manual")
                    and not self._in_dwell(st)
                    and value <= self._sat_release
                ):
                    self._reset_satiety(st)
                    logger.info(
                        f"[DualPersona] 情绪回落至 {value:g} 且驻留期满 → 回到人格 A"
                    )
                    return "a"

        # ---- 5) 深夜：手动优先，已在目标人格时什么都不做 ----
        if await self._check_night_time():
            tag = self._current_night_tag()
            if st["night_tag"] != tag:
                # 只记录「这一夜已处理过」，绝不重置 manual —— 手动选择永久优先
                st["night_tag"] = tag
            if st["manual"]:
                logger.debug(
                    "[DualPersona] 深夜规则跳过：用户已手动锁定在 "
                    f"{self._get_persona_name(st['key'])}"
                )
            elif st["key"] == self._night_target:
                # 已在目标人格：不碰数值、不设驻留，避免自己触发自己
                pass
            else:
                logger.debug(
                    f"[DualPersona] 深夜模式生效，倾向人格 {self._night_target.upper()}"
                )
                if self._satiety_enabled:
                    st["satiety"] = float(self._sat_threshold)
                    st["sat_ts"] = time.time()
                    st["dwell_until"] = (
                        time.time() + self._min_dwell if self._min_dwell else 0.0
                    )
                return self._night_target
        return None

    def _parse_hhmm(self, value: str):
        try:
            h, m = str(value).strip().split(":")
            return datetime.now().replace(
                hour=int(h), minute=int(m), second=0, microsecond=0
            ).time()
        except Exception:
            return None

    def _sweep_idle_sessions(self) -> int:
        """周期性检查：闲置的会话也要按时间衰减并回退（纯内存操作，不写库）"""
        if not self._satiety_enabled:
            return 0
        changed = 0
        now = time.time()
        for st in list(self._sessions.values()):
            try:
                if st.get("key") != "b":
                    continue
                if st.get("manual"):
                    continue        # 手动选定的人格不随时间被赶走
                if self._in_dwell(st):
                    continue
                self._decay_satiety(st)
                if float(st.get("satiety") or 0) <= self._sat_release:
                    st["key"] = "a"
                    st["satiety"] = 0.0
                    st["dwell_until"] = 0.0
                    st["sat_ts"] = now
                    changed += 1
                    logger.info(
                        "[DualPersona] 闲置回退：情绪值耗尽且驻留期满 → 回到人格 A"
                    )
            except Exception:
                logger.debug("[DualPersona] 闲置扫描异常", exc_info=True)
        return changed

    async def _idle_sweeper(self) -> None:
        """每 5 分钟扫一次，让「没人说话」时也能自然冷淡下来"""
        while True:
            try:
                await asyncio.sleep(300)
                self._sweep_idle_sessions()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug("[DualPersona] 闲置扫描任务异常", exc_info=True)

    async def _check_night_time(self) -> bool:
        """检查当前是否处在深夜区间"""
        start = self._parse_hhmm(self._night_start_time)
        end = self._parse_hhmm(self._night_end_time)
        if not start or not end:
            return False
        now = datetime.now().time()
        if start <= end:
            return start <= now < end
        return now >= start or now < end      # 跨天，如 23:00–04:00

    def _current_night_tag(self) -> str:
        """把跨午夜的深夜区间归到同一个日期，用于「每夜只切一次」"""
        start = self._parse_hhmm(self._night_start_time)
        now = datetime.now()
        if start and now.time() < start:
            return (now.date() - timedelta(days=1)).isoformat()
        return now.date().isoformat()

    # ==================================================================
    # 指令：/切换人格
    # ==================================================================
    @filter.command("切换人格")
    async def switch_persona(self, event: AstrMessageEvent, persona_selector: str = ""):
        """切换当前激活的人格（仅管理员）。"""
        if not self._persona_a_id and not self._persona_b_id:
            yield event.plain_result(
                "⚠️ 尚未配置任何人格。\n\n"
                "请先在 AstrBot WebUI 的「人格管理」中创建至少两个人设，\n"
                "然后在「插件配置 → 双人格热切换」中选择对应的人格。"
            )
            return

        if not self._manual_switch_enabled:
            yield event.plain_result("🚫 手动切换功能已被禁用。")
            return

        if not self._is_admin(event):
            yield event.plain_result("🚫 仅管理员可切换人格。\n如需切换，请联系管理员。")
            return

        await self._ensure_persona_data_loaded()
        st = await self._state(event)
        a_name = self._get_persona_name("a")
        b_name = self._get_persona_name("b")

        selector = (persona_selector or "").strip()
        if selector:
            matched = self._match_persona(selector)
            if matched is None:
                yield event.plain_result(
                    f"❌ 未找到人格「{selector}」。\n"
                    f"当前可用：\n  • {a_name}\n  • {b_name}"
                )
                return
            old_key = st["key"]
            await self._do_switch(event, st, matched, manual=True)
            async for msg in self._emit_switch_lines(event, old_key, matched):
                yield msg
            return

        cur_mark_a = " ✅ (当前)" if st["key"] == "a" else ""
        cur_mark_b = " ✅ (当前)" if st["key"] == "b" else ""
        yield event.plain_result(
            f"🎭 **双人格切换**\n\n"
            f"**人格 A** — {a_name}{cur_mark_a}\n"
            f"> {self._get_prompt_preview('a')}...\n\n"
            f"**人格 B** — {b_name}{cur_mark_b}\n"
            f"> {self._get_prompt_preview('b')}...\n\n"
            f"💡 使用 `/切换人格 A` 或 `/切换人格 B` 来切换。\n"
            f"💡 切换后**不会清空聊天记录**，两个人格共用记忆。"
        )

    # ==================================================================
    # 指令：/当前人格（含完整生效设置）
    # ==================================================================
    @filter.command("当前人格")
    async def current_persona(self, event: AstrMessageEvent):
        """查看当前会话的人格与生效设置"""
        await self._ensure_persona_data_loaded()
        st = await self._state(event)

        is_night = await self._check_night_time()
        auto = "开" if self._auto_switch_enabled else "关"
        manual = "开" if self._manual_switch_enabled else "关"
        handoff = "开" if self._natural_handoff else "关"
        aware = "开" if self._self_aware else "关"
        night_line = (
            f"{'🌙 生效中' if is_night else '⏰ 未生效'}"
            f"（{self._night_start_time}–{self._night_end_time} → "
            f"{self._get_persona_name(self._night_target)}）"
            if self._auto_switch_enabled
            else "已关闭"
        )
        a_words = "、".join([w for w in self._trigger_words_a if w]) or "无"
        b_words = "、".join([w for w in self._trigger_words_b if w]) or "无"
        scope_label = (
            "全局互通（私聊 + 所有群聊共用一个当前人格）"
            if self._state_scope == "global"
            else "按会话隔离"
        )
        word_perm = "仅管理员" if self._auto_switch_admin_only else "所有人"

        yield event.plain_result(
            f"🎭 **当前人格状态**\n\n"
            f"作用范围：{scope_label}\n"
            f"标识：`{self._state_key(event)}`\n"
            f"当前激活：**{self._get_persona_name(st['key'])}**"
            f"（人格 {st['key'].upper()}）\n"
            f"深夜被手动锁定：{'是' if st['manual'] else '否'}\n\n"
            f"**生效设置**\n"
            f"• 自动切换：{auto}\n"
            f"• 关键词触发权限：{word_perm}\n"
            f"• 模型自主切换：{'开' if self._llm_switch_enabled else '关'}\n"
            f"• 切换提示：{self._switch_notice_mode}\n"
            f"• 情绪值：{st.get('satiety', 0):g}/{self._sat_threshold:g}"
            f"（弱词 +{self._sat_weak_add:g}，驻留 {int(self._min_dwell // 60)} 分"
            f"{'，剩余 %d 秒' % max(0, int(float(st.get('dwell_until') or 0) - time.time())) if self._in_dwell(st) else ''}）\n"
            f"• 提示词占用：约 {st.get('metrics', {}).get('total_tokens', 0)} tokens"
            f"（人设 {st.get('metrics', {}).get('persona_tokens', 0)}"
            f" + 状态 {st.get('metrics', {}).get('state_tokens', 0)}）\n"
            f"• 深夜模式：{night_line}"
            f"{'（当前已在该人格，无需切换）' if is_night and st['key'] == self._night_target else ''}\n"
            f"• 手动切换：{manual}\n"
            f"• 自然接管提示：{handoff}\n"
            f"• AI 自知（提示词注入）：{aware}\n"
            f"• 切换台词：{self._switch_lines_mode}\n"
            f"• 触发词 a→{self._get_persona_name('b')}：{a_words}\n"
            f"• 触发词 b→{self._get_persona_name('a')}：{b_words}\n"
            f"• 强词→B：{'、'.join([w for w in self._strong_words_to_b if w]) or '无'}\n"
            f"• 强词→A：{'、'.join([w for w in self._strong_words_to_a if w]) or '无'}\n"
            f"• 弱词阈值 {self._weak_need} 个｜冷却 {int(self._switch_cooldown // 60)} 分钟\n\n"
            f"可用人格：\n  • A — {self._get_persona_name('a')}\n"
            f"  • B — {self._get_persona_name('b')}"
        )

    # ==================================================================
    # 指令：/人格预览（核对人设是否真的注入）
    # ==================================================================
    @filter.command("人格预览")
    async def preview_persona(self, event: AstrMessageEvent, limit: str = "1200"):
        """查看本轮实际注入的 system prompt（仅管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("🚫 仅管理员可查看人格注入内容。")
            return
        await self._ensure_persona_data_loaded()
        st = await self._state(event)
        key = st["key"]
        data = await self._get_persona_data(key)
        if not data or not data.get("prompt"):
            yield event.plain_result(
                "⚠️ 当前人格读不到内容，所以什么都没注入。\n"
                "请检查插件配置里选择的人格是否还在 AstrBot 人格管理中。"
            )
            return
        try:
            n = max(200, min(int(limit), 4000))
        except Exception:
            n = 1200
        name = data.get("name", key.upper())
        full = self._build_system_prompt(st, name, data["prompt"], "")
        yield event.plain_result(
            f"🔍 **人格注入预览**\n"
            f"人格：{name}（{key.upper()}）| 模式：{self._prompt_mode}\n"
            f"人设长度：{len(data['prompt'])} 字 | 拼装后：{len(full)} 字\n"
            f"（下面是去掉平台提示词后的完整内容，最多 {n} 字）\n\n{full[:n]}"
        )

    # ==================================================================
    # 指令：/人格列表
    # ==================================================================
    @filter.command("人格列表")
    async def list_personas(self, event: AstrMessageEvent):
        """列出当前可用的两个人格"""
        await self._ensure_persona_data_loaded()
        st = await self._state(event)
        yield event.plain_result(
            f"🎭 **可用人格列表**\n\n"
            f"当前激活：**{self._get_persona_name(st['key'])}**\n\n"
            f"─── 人格 A ───\n名称：{self._get_persona_name('a')}\n"
            f"描述：{self._get_prompt_preview('a')}...\n\n"
            f"─── 人格 B ───\n名称：{self._get_persona_name('b')}\n"
            f"描述：{self._get_prompt_preview('b')}...\n\n"
            f"💡 使用 `/切换人格 A` 或 `/切换人格 B` 来切换。"
        )

    # ==================================================================
    # 快捷切换：/<人格名>来了
    # ==================================================================
    def _wake_prefixes(self) -> list[str]:
        try:
            p = (self.context.get_config() or {}).get("wake_prefix")
            if isinstance(p, str):
                p = [p]
            if isinstance(p, list) and p:
                return [str(x) for x in p if str(x)]
        except Exception:
            pass
        return ["/"]

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_switch_trigger_message(self, event: AstrMessageEvent):
        """监听 /<人格显示名><后缀> 形式的快捷切换指令"""
        if not self._manual_switch_enabled:
            return
        if not (self._persona_a_id or self._persona_b_id):
            return

        msg = (event.message_str or "").strip()
        prefix = next((p for p in self._wake_prefixes() if msg.startswith(p)), None)
        if prefix is None:
            return
        body = msg[len(prefix):].replace(" ", "").replace("\u3000", "").strip()
        if not body:
            return

        await self._ensure_persona_data_loaded()
        suffix = (self._switch_trigger_suffix or "来了").strip()
        matched = None
        for key in ("a", "b"):
            name = self._get_persona_name(key).strip()
            if name and name != "未设置" and body == f"{name}{suffix}":
                matched = key
                break
        if matched is None:
            return

        if not self._is_admin(event):
            yield event.plain_result("🚫 仅管理员可切换人格。")
            event.stop_event()
            return

        st = await self._state(event)
        old_key = st["key"]
        await self._do_switch(event, st, matched, manual=True)
        async for m in self._emit_switch_lines(event, old_key, matched):
            yield m
        event.stop_event()

    # ==================================================================
    # 切换台词
    # ==================================================================
    async def _emit_switch_lines(
        self, event: AstrMessageEvent, old_key: str, new_key: str
    ):
        """根据配置发送退场语 + 登场语"""
        old_name = self._get_persona_name(old_key)
        new_name = self._get_persona_name(new_key)
        old_pid = self._get_persona_id(old_key)
        new_pid = self._get_persona_id(new_key)

        if self._switch_lines_mode == "none":
            yield event.plain_result(f"✨ 人格已切换为「{new_name}」（聊天上下文已保留）。")
            return

        if self._switch_lines_mode == "custom":
            exit_line = self._get_custom_line(old_pid, "exit_line") or ""
            enter_line = self._get_custom_line(new_pid, "enter_line") or ""
        elif self._switch_lines_mode == "personalized":
            # 内置第二套措辞（与具体人格设定无关，只是换一种语气）
            exit_line = "……{name}先失陪一下，{next_name}会陪着你的。"
            enter_line = "换我了。{name}在，别担心。"
        else:  # default
            exit_line = "{name}先退下了，{next_name}会陪您的。"
            enter_line = "{name}来啦~"

        if exit_line:
            yield event.plain_result(
                self._format_switch_line(exit_line, old_name, new_name, old_pid, new_pid)
            )
        if enter_line:
            yield event.plain_result(
                self._format_switch_line(enter_line, new_name, old_name, new_pid, old_pid)
            )

    def _get_custom_line(self, persona_id: str, line_type: str) -> str | None:
        if not persona_id or not isinstance(self._switch_lines, dict):
            return None
        persona_cfg = self._switch_lines.get(persona_id)
        if not isinstance(persona_cfg, dict):
            return None
        value = persona_cfg.get(line_type)
        return value if isinstance(value, str) else None

    def _format_switch_line(
        self, template: str, name: str, other_name: str, persona_id: str, other_pid: str
    ) -> str:
        return (
            str(template)
            .replace("{name}", name)
            .replace("{next_name}", other_name)
            .replace("{prev_name}", other_name)
            .replace("{persona_id}", persona_id)
            .replace("{next_persona_id}", other_pid)
            .replace("{prev_persona_id}", other_pid)
        )

    def _get_persona_id(self, key: str) -> str:
        return self._persona_a_id if key == "a" else self._persona_b_id

    # ==================================================================
    # 人格数据读取
    # ==================================================================
    async def _get_persona_data(self, key: str) -> dict | None:
        """带 TTL 的人格缓存，改完人设最多 60 秒后生效"""
        hit = self._persona_cache.get(key)
        if hit and time.monotonic() - hit[0] < self._persona_ttl:
            return hit[1]
        pid = self._persona_a_id if key == "a" else self._persona_b_id
        data = await self._fetch_persona_by_id(pid)
        if data:
            self._persona_cache[key] = (time.monotonic(), data)
        return data

    async def _ensure_persona_data_loaded(self, force: bool = False) -> None:
        if force:
            self._persona_cache.clear()
        if self._persona_a_id:
            await self._get_persona_data("a")
        if self._persona_b_id:
            await self._get_persona_data("b")

    async def _fetch_persona_by_id(self, persona_id: str) -> dict | None:
        """从 AstrBot 的 PersonaManager 读取人格，兼容多种版本 API"""
        if not persona_id:
            return None

        pm = getattr(self.context, "persona_manager", None)
        if pm is None:
            logger.warning("[DualPersona] context.persona_manager 不存在，无法读取人格")
            return None

        # 策略 1：get_persona(persona_id) -> Persona 对象
        if hasattr(pm, "get_persona"):
            try:
                parsed = self._parse_persona_result(
                    await pm.get_persona(persona_id), persona_id
                )
                if parsed:
                    return parsed
            except Exception:
                logger.debug(f"[DualPersona] get_persona 失败: {persona_id}", exc_info=True)

        # 策略 2：字典式访问
        try:
            parsed = self._parse_persona_result(pm[persona_id], persona_id)
            if parsed:
                return parsed
        except Exception:
            logger.debug(f"[DualPersona] pm[id] 失败: {persona_id}", exc_info=True)

        # 策略 3：get 方法
        if hasattr(pm, "get"):
            try:
                result = pm.get(persona_id)
                if asyncio.iscoroutine(result):
                    result = await result
                parsed = self._parse_persona_result(result, persona_id)
                if parsed:
                    return parsed
            except Exception:
                logger.debug(f"[DualPersona] pm.get 失败: {persona_id}", exc_info=True)

        # 策略 4：personas_v3 列表（dict）
        try:
            for p in getattr(pm, "personas_v3", None) or []:
                if isinstance(p, dict) and (
                    p.get("name") == persona_id or p.get("id") == persona_id
                ):
                    return {
                        "name": p.get("name", persona_id),
                        "prompt": p.get("prompt", "") or p.get("system_prompt", ""),
                        "raw": p,
                    }
        except Exception:
            logger.debug("[DualPersona] personas_v3 扫描失败", exc_info=True)

        # 策略 5：personas 列表（对象）
        try:
            for p in getattr(pm, "personas", None) or []:
                if getattr(p, "persona_id", None) == persona_id:
                    name = (
                        getattr(p, "name", None)
                        or getattr(p, "persona_id", None)
                        or persona_id
                    )
                    return {
                        "name": name,
                        "prompt": getattr(p, "system_prompt", "")
                        or getattr(p, "prompt", ""),
                        "raw": p,
                    }
        except Exception:
            logger.debug("[DualPersona] personas 扫描失败", exc_info=True)

        logger.warning(
            f"[DualPersona] 无法读取人格 {persona_id}（API 不匹配或该人设已被删除）。"
            f"请在 插件配置 或 AstrBot 人格管理中确认它仍然存在。"
        )
        return None

    def _parse_persona_result(self, result: object, persona_id: str) -> dict | None:
        if result is None:
            return None
        if isinstance(result, dict):
            prompt = result.get("prompt") or result.get("system_prompt") or ""
            if not prompt:
                return None
            return {"name": result.get("name") or persona_id, "prompt": prompt, "raw": result}
        prompt = getattr(result, "system_prompt", None) or getattr(result, "prompt", "")
        if not prompt:
            return None
        name = (
            getattr(result, "name", None)
            or getattr(result, "persona_id", None)
            or persona_id
        )
        return {"name": name, "prompt": prompt, "raw": result}

    def _cached(self, key: str) -> dict | None:
        hit = self._persona_cache.get(key)
        return hit[1] if hit else None

    def _get_persona_name(self, key: str) -> str:
        data = self._cached(key)
        fallback = self._persona_a_id if key == "a" else self._persona_b_id
        return (data or {}).get("name", "") or fallback or "未设置"

    def _get_prompt_preview(self, key: str, limit: int = 80) -> str:
        data = self._cached(key)
        if not data:
            return "(未配置或加载失败)"
        prompt = data.get("prompt", "")
        return prompt[:limit] if prompt else "(无内容)"

    def _match_persona(self, name: str) -> str | None:
        """按 key / 名称匹配人格，优先精确匹配再模糊"""
        name_lower = name.strip().lower()
        if name_lower in ("a", "人格a"):
            return "a" if self._persona_a_id else None
        if name_lower in ("b", "人格b"):
            return "b" if self._persona_b_id else None

        candidates = []
        for key in ("a", "b"):
            display = self._get_persona_name(key).lower()
            if display and display != "未设置":
                candidates.append((key, display))
        for key, display in candidates:      # 先精确
            if name_lower == display:
                return key
        for key, display in candidates:      # 再包含
            if name_lower in display or display in name_lower:
                return key
        return None

    # ==================================================================
    # 权限
    # ==================================================================
    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            sender = str(event.get_sender_id())
        except Exception:
            return False
        if sender in self._admin_uids:
            return True
        try:
            g = self.context.get_config() or {}
            admins = g.get("admins_id") or []
            if isinstance(admins, str):
                admins = admins.split(",")
            return sender in {str(a).strip() for a in admins}
        except Exception:
            logger.debug("[DualPersona] 读取全局 admins_id 失败", exc_info=True)
            return False

    # ==================================================================
    # 状态面板后端（WebUI 插件可视化视图）
    # ==================================================================
    def _register_web_api(self) -> None:
        reg = getattr(self.context, "register_web_api", None)
        if not callable(reg) or not _WEB_API_AVAILABLE:
            logger.info("[DualPersona] 当前 AstrBot 版本不支持插件 Web API，状态面板不可用")
            return
        try:
            reg(f"/{PLUGIN_NAME}/state", self._api_state, ["GET"], "双人格状态与生效设置")
            reg(f"/{PLUGIN_NAME}/refresh", self._api_refresh, ["POST"], "重新读取人格")
            reg(f"/{PLUGIN_NAME}/settings", self._api_save_settings, ["POST"], "保存插件设置")
            logger.info("[DualPersona] 状态面板 API 已注册")
        except Exception:
            logger.warning("[DualPersona] 注册 Web API 失败", exc_info=True)

    async def _api_state(self):
        if not _WEB_API_AVAILABLE:
            return {"error": "web api unavailable"}
        await self._ensure_persona_data_loaded()
        night = await self._check_night_time()
        sessions = sorted(
            self._sessions.items(), key=lambda kv: kv[1].get("updated", 0), reverse=True
        )[:50]
        return json_response(
            {
                "plugin": PLUGIN_NAME,
                "version": PLUGIN_VERSION,
                "night": {
                    "start": self._night_start_time,
                    "end": self._night_end_time,
                    "target": self._night_target,
                    "active": night,
                    "tag": self._current_night_tag(),
                },
                "personas": {
                    key: {
                        "id": self._get_persona_id(key),
                        "name": self._get_persona_name(key),
                        "configured": bool(self._get_persona_id(key)),
                        "loaded": self._cached(key) is not None,
                        "prompt_preview": self._get_prompt_preview(key, 240),
                        "prompt_length": len((self._cached(key) or {}).get("prompt", "")),
                        "prompt_tokens": estimate_tokens(
                            (self._cached(key) or {}).get("prompt", "")
                        ),
                    }
                    for key in ("a", "b")
                },
                "effective_config": {
                    "state_scope": self._state_scope,
                    "switch_notice_mode": self._switch_notice_mode,
                    "llm_switch": self._llm_switch_enabled,
                    "llm_switch_rules": self._llm_switch_rules,
                    "auto_switch_admin_only": self._auto_switch_admin_only,
                    "auto_switch": self._auto_switch_enabled,
                    "manual_switch": self._manual_switch_enabled,
                    "night_start": self._night_start_time,
                    "night_end": self._night_end_time,
                    "night_target": self._night_target,
                    "night_target_name": self._get_persona_name(self._night_target),
                    "strong_words_to_b": [w for w in self._strong_words_to_b if w],
                    "strong_words_to_a": [w for w in self._strong_words_to_a if w],
                    "weak_need": self._weak_need,
                    "switch_cooldown_min": int(self._switch_cooldown // 60),
                    "trigger_words_a": [w for w in self._trigger_words_a if w],
                    "trigger_words_b": [w for w in self._trigger_words_b if w],
                    "natural_handoff": self._natural_handoff,
                    "switch_lines_mode": self._switch_lines_mode,
                    "switch_trigger_suffix": self._switch_trigger_suffix,
                    "self_aware": self._self_aware,
                    "self_aware_include_triggers": self._self_aware_triggers,
                    "self_aware_compact": self._self_aware_compact,
                    "allow_single_char_words": self._allow_single_char,
                    "satiety_show_in_prompt": self._sat_show,
                    "admin_count": len(self._admin_uids),
                },
                "satiety": {
                    "enabled": self._satiety_enabled,
                    "threshold": self._sat_threshold,
                    "weak_add": self._sat_weak_add,
                    "mood_add": self._sat_mood_add,
                    "decay_msg": self._sat_decay_msg,
                    "decay_10min": self._sat_decay_10min,
                    "release": self._sat_release,
                    "min_dwell_min": int(self._min_dwell // 60),
                },
                "prompt_budget": {
                    "persona_tokens": {
                        key: estimate_tokens((self._cached(key) or {}).get("prompt", ""))
                        for key in ("a", "b")
                    },
                    "state_block_tokens": estimate_tokens(
                        self._state_block({"key": "a"})
                    ) if self._self_aware else 0,
                    "mode": self._prompt_mode,
                    "self_aware_compact": self._self_aware_compact,
                },
                "sessions": [
                    {
                        "umo": "全局（私聊 + 所有群聊）" if umo == "global" else umo,
                        "scope_key": umo,
                        "persona_key": st["key"],
                        "persona_name": self._get_persona_name(st["key"]),
                        "manual_lock": bool(st.get("manual")),
                        "tokens": st.get("metrics", {}).get("total_tokens", 0),
                        "satiety": round(float(st.get("satiety") or 0), 1),
                        "dwell_remain": max(
                            0, int(float(st.get("dwell_until") or 0) - time.time())
                        ),
                        "updated": datetime.fromtimestamp(
                            st.get("updated", 0)
                        ).strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    for umo, st in sessions
                ],
                "note": "会话列表来自插件运行期内存，重启后需等各会话再发一条消息才会重新出现。",
            }
        )

    # 允许在面板里改的字段（白名单，避免误改人格 ID 之类的敏感项）
    _EDITABLE_FIELDS = {
        "state_scope": ("str", ("global", "session")),
        "auto_switch_enabled": ("bool", None),
        "manual_switch_enabled": ("bool", None),
        "auto_switch_admin_only": ("bool", None),
        "satiety_enabled": ("bool", None),
        "satiety_threshold": ("num", (10, 100)),
        "satiety_weak_add": ("num", (1, 100)),
        "satiety_mood_add": ("num", (0, 100)),
        "satiety_decay_msg": ("num", (0, 50)),
        "satiety_decay_10min": ("num", (0, 100)),
        "satiety_release": ("num", (0, 100)),
        "min_dwell_min": ("num", (0, 720)),
        "satiety_show_in_prompt": ("bool", None),
        "allow_single_char_words": ("bool", None),
        "self_aware": ("bool", None),
        "self_aware_compact": ("bool", None),
        "self_aware_include_triggers": ("bool", None),
        "llm_switch_enabled": ("bool", None),
        "night_start_time": ("str", None),
        "night_end_time": ("str", None),
        "night_target": ("str", ("a", "b")),
        "switch_notice_mode": ("str", ("off", "always", "auto_only", "manual_only")),
        "switch_notice_template": ("str", None),
        "switch_trigger_suffix": ("str", None),
        "prompt_mode": ("str", ("prepend", "append", "override")),
        "natural_handoff": ("bool", None),
        "weak_need": ("num", (1, 10)),
    }

    def _apply_setting(self, key: str, value) -> str | None:
        """校验并应用单个设置项，返回错误信息或 None"""
        spec = self._EDITABLE_FIELDS.get(key)
        if not spec:
            return f"不允许修改：{key}"
        kind, allowed = spec
        if kind == "bool":
            if not isinstance(value, bool):
                return f"{key} 需要布尔值"
        elif kind == "num":
            try:
                num = float(value)
            except Exception:
                return f"{key} 需要数字"
            if allowed and not (allowed[0] <= num <= allowed[1]):
                return f"{key} 需在 {allowed[0]}~{allowed[1]} 之间"
            value = int(num) if float(num).is_integer() else num
        else:
            value = str(value)
            if allowed and value not in allowed:
                return f"{key} 只能是 {allowed} 之一"
        cfg = getattr(self, "_raw_config", None)
        if cfg is None:
            return "当前环境不支持保存配置（未拿到 AstrBotConfig）"
        try:
            cfg[key] = value
        except Exception:
            return "写入配置失败"
        return None

    def _reload_runtime(self) -> None:
        """把配置重新读进运行参数（不重建对象，只刷新字段）"""
        cfg = getattr(self, "_raw_config", None)
        if cfg is None:
            return
        self._auto_switch_enabled = bool(cfg.get("auto_switch_enabled", True))
        self._manual_switch_enabled = bool(cfg.get("manual_switch_enabled", True))
        self._auto_switch_admin_only = bool(cfg.get("auto_switch_admin_only", True))
        self._state_scope = (cfg.get("state_scope", "global") or "global").strip()
        if self._state_scope not in ("global", "session"):
            self._state_scope = "global"
        self._night_start_time = cfg.get("night_start_time", "23:00") or "23:00"
        self._night_end_time = cfg.get("night_end_time", "04:00") or "04:00"
        self._night_target = (cfg.get("night_target", "a") or "a").strip().lower()
        if self._night_target not in ("a", "b"):
            self._night_target = "a"
        self._self_aware = bool(cfg.get("self_aware", True))
        self._self_aware_compact = bool(cfg.get("self_aware_compact", True))
        self._self_aware_triggers = bool(cfg.get("self_aware_include_triggers", False))
        self._llm_switch_enabled = bool(cfg.get("llm_switch_enabled", False))
        self._switch_notice_mode = (cfg.get("switch_notice_mode", "auto_only") or "auto_only").strip()
        self._switch_notice_template = (
            cfg.get("switch_notice_template", "— 现在为「{name}」（人格 {key}）")
            or "— 现在为「{name}」（人格 {key}）"
        ).strip()
        self._switch_trigger_suffix = cfg.get("switch_trigger_suffix", "来了") or "来了"
        self._prompt_mode = (cfg.get("prompt_mode", "prepend") or "prepend").strip()
        if self._prompt_mode not in ("prepend", "append", "override"):
            self._prompt_mode = "prepend"
        self._natural_handoff = bool(cfg.get("natural_handoff", True))
        self._satiety_enabled = bool(cfg.get("satiety_enabled", True))
        self._allow_single_char = bool(cfg.get("allow_single_char_words", True))
        self._sat_show = bool(cfg.get("satiety_show_in_prompt", False))
        try:
            self._weak_need = max(1, int(cfg.get("weak_need", 2) or 2))
        except Exception:
            self._weak_need = 2
        for attr, key, lo, hi, default in (
            ("_sat_threshold", "satiety_threshold", 10, 100, 100),
            ("_sat_weak_add", "satiety_weak_add", 1, 100, 25),
            ("_sat_mood_add", "satiety_mood_add", 0, 100, 5),
            ("_sat_decay_msg", "satiety_decay_msg", 0, 50, 1),
            ("_sat_decay_10min", "satiety_decay_10min", 0, 100, 10),
            ("_sat_release", "satiety_release", 0, 100, 20),
        ):
            try:
                setattr(self, attr, max(lo, min(hi, float(cfg.get(key, default) or default))))
            except Exception:
                setattr(self, attr, float(default))
        try:
            self._min_dwell = max(0.0, min(720.0, float(cfg.get("min_dwell_min", 20) or 0))) * 60
        except Exception:
            self._min_dwell = 1200.0

    async def _api_save_settings(self):
        if not _WEB_API_AVAILABLE:
            return {"error": "web api unavailable"}
        try:
            payload = await request.json(default={})
        except Exception:
            payload = {}
        if not isinstance(payload, dict) or not payload:
            return error_response("没有收到任何设置")
        errors = {}
        applied = {}
        for key, value in payload.items():
            err = self._apply_setting(key, value)
            if err:
                errors[key] = err
            else:
                applied[key] = value
        if applied:
            cfg = getattr(self, "_raw_config", None)
            try:
                cfg.save_config()
            except Exception as exc:
                return error_response(f"写入配置文件失败：{exc}")
            self._reload_runtime()
            await self._ensure_persona_data_loaded(force=False)
        return json_response(
            {"saved": list(applied.keys()), "errors": errors, "ok": not errors}
        )

    async def _api_refresh(self):
        if not _WEB_API_AVAILABLE:
            return {"error": "web api unavailable"}
        await self._ensure_persona_data_loaded(force=True)
        return json_response({"refreshed": True})

    # ==================================================================
    # 生命周期
    # ==================================================================
    async def initialize(self) -> None:
        """插件加载完成（部分版本会调用，不调用也不影响功能）"""
        if self._satiety_enabled and self._min_dwell >= 0:
            try:
                self._sweeper_task = asyncio.create_task(self._idle_sweeper())
            except Exception:
                logger.debug("[DualPersona] 启动闲置扫描失败", exc_info=True)
        logger.info(
            f"[DualPersona] 加载完成 | A: {self._persona_a_id or '(未设置)'} | "
            f"B: {self._persona_b_id or '(未设置)'} | 管理员: {len(self._admin_uids)} 人"
        )

    async def terminate(self) -> None:
        for umo, st in list(self._sessions.items()):
            try:
                await self.put_kv_data(f"persona:{umo}", st["key"])
            except Exception:
                logger.debug(f"[DualPersona] 保存会话状态失败: {umo}", exc_info=True)
        sweeper = getattr(self, "_sweeper_task", None)
        if sweeper:
            sweeper.cancel()
        for task in list(getattr(self, "_bg_tasks", ())):
            task.cancel()
        if self._night_timer_task:
            self._night_timer_task.cancel()
        logger.info("[DualPersona] 状态已保存，插件已卸载。")
