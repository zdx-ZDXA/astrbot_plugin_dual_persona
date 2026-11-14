"""
astrbot_plugin_dual_persona — 双人格热切换插件
==============================================
一体双生，共用记忆。直接复用 AstrBot 已配置的「人格」(Persona)，
在两个人设之间一键切换，共享全部聊天上下文。

核心机制：
- 通过 on_llm_request 钩子，用当前激活人格的 prompt 替换 system_prompt
- 人格数据从 AstrBot 内置的 PersonaManager 实时读取
- 切换人格仅改变 system_prompt，绝不触碰会话上下文（不调用 reset）
- 切换时可选剧情化过场台词，退场语属于旧人格，登场语属于新人格
- 自然接管提示：切换后首次 LLM 请求自动注入一次性引导，让新人格无缝接话
- 使用 KV 存储持久化当前激活的人格（重启后恢复）
"""

import re

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.provider import ProviderRequest


class DualPersona(Star):
    """双人格热切换插件 — 读取 AstrBot 内置人设"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)

        cfg = config if config else {}

        # 用户通过 WebUI（_special: select_persona）选择的两个人格的 ID
        self._persona_a_id: str = cfg.get("persona_a_id", "") or ""
        self._persona_b_id: str = cfg.get("persona_b_id", "") or ""

        # 切换台词模式：none / default / personalized / custom
        self._switch_lines_mode: str = cfg.get("switch_lines_mode", "personalized")

        # 是否启用作者专属模板
        self._enable_author_template: bool = cfg.get("enable_author_template", True)

        # 自定义切换台词：{persona_id: {"exit_line": "...", "enter_line": "..."}}
        self._switch_lines: dict = cfg.get("switch_lines", {}) or {}

        # 是否启用自然接管提示
        self._natural_handoff: bool = cfg.get("natural_handoff", True)

        # 快捷切换指令触发后缀，例如「来了」
        self._switch_trigger_suffix: str = cfg.get("switch_trigger_suffix", "来了") or "来了"

        # 当前激活的人格标识：'a' 或 'b'
        self._current_key: str = "a"

        # 记录上一次完成自然接管的人格 key，用于判断切换后首次请求
        self._last_handoff_key: str = "a"

        # 缓存两个人格的数据（name + prompt），启动后首次使用时加载
        self._persona_a_data: dict | None = None
        self._persona_b_data: dict | None = None

        if not self._persona_a_id and not self._persona_b_id:
            logger.warning(
                "[DualPersona] 尚未在 WebUI 插件配置中选择人格，"
                "请前往 设置 → 插件配置 → 双人格热切换 选择两个人设。"
            )

    # ------------------------------------------------------------------
    # LLM 请求钩子 — 注入当前人格的 system_prompt + 自然接管提示
    # ------------------------------------------------------------------
    @filter.on_llm_request()
    async def inject_persona(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """
        每次 LLM 请求前，用当前激活人格的 prompt 改写 system_prompt。
        **不会清空上下文** — 仅改写 system prompt，历史对话完整保留。
        """
        persona_data = await self._get_active_persona_data()
        if persona_data is None:
            return

        persona_prompt = persona_data.get("prompt", "")
        if not persona_prompt:
            return

        original_sp = req.system_prompt

        # 当前人格 prompt 放最前，原始 system_prompt 作为补充规则保留
        if original_sp and original_sp.strip():
            req.system_prompt = (
                f"{persona_prompt}\n\n"
                f"---\n"
                f"以下是额外的行为准则，同样需要遵守：\n"
                f"{original_sp}"
            )
        else:
            req.system_prompt = persona_prompt

        # 自然接管：切换后的第一次请求，附加一次性提示
        if self._natural_handoff and self._current_key != self._last_handoff_key:
            self._inject_handoff_prompt(req)
            self._last_handoff_key = self._current_key

        logger.debug(
            f"[DualPersona] 注入人格: {persona_data.get('name', '?')} -> "
            f"session={event.unified_msg_origin}"
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

    # ------------------------------------------------------------------
    # 指令：/切换人格
    # ------------------------------------------------------------------
    @filter.command("切换人格")
    async def switch_persona(
        self, event: AstrMessageEvent, persona_selector: str | None = None
    ):
        """
        切换当前激活的人格。

        用法：
          /切换人格              —— 查看人格信息和切换方式
          /切换人格 A             —— 切换到人格 A
          /切换人格 B             —— 切换到人格 B
          /切换人格 <人格名称>    —— 按名称模糊匹配并切换
          
        ⚠️ 注意：此操作需要管理员权限。
        """
        # 检查是否已配置
        if not self._persona_a_id and not self._persona_b_id:
            yield event.plain_result(
                "⚠️ 尚未配置任何人格。\n\n"
                "请先在 AstrBot WebUI 的「人格管理」中创建至少两个人设，\n"
                "然后在「插件配置 → 双人格热切换」中选择对应的两个人格。"
            )
            return

        # 检查管理员权限
        if not self._is_admin(event):
            yield event.plain_result(
                "🚫 仅管理员可切换人格。\n"
                "如需切换，请联系管理员。"
            )
            return

        # 确保名称已加载
        await self._ensure_persona_data_loaded()

        a_name = self._get_persona_name("a")
        b_name = self._get_persona_name("b")

        if persona_selector:
            # -- 直接切换模式 --
            matched = self._match_persona(persona_selector)
            if matched is None:
                yield event.plain_result(
                    f"❌ 未找到人格「{persona_selector}」。\n"
                    f"当前可用：\n"
                    f"  • {a_name}\n"
                    f"  • {b_name}"
                )
                return

            # 切换前保存旧人格信息，用于生成退场/登场语
            old_key = self._current_key
            new_key = matched

            await self._do_switch(new_key)

            # 发送剧情化切换台词
            async for msg in self._emit_switch_lines(event, old_key, new_key):
                yield msg
            return

        # -- 无参数模式：展示人格信息 --
        a_prompt_preview = self._get_prompt_preview("a")
        b_prompt_preview = self._get_prompt_preview("b")
        current_mark_a = " ✅ (当前)" if self._current_key == "a" else ""
        current_mark_b = " ✅ (当前)" if self._current_key == "b" else ""

        yield event.plain_result(
            f"🎭 **双人格切换**\n\n"
            f"**人格 A** — {a_name}{current_mark_a}\n"
            f"> {a_prompt_preview}...\n\n"
            f"**人格 B** — {b_name}{current_mark_b}\n"
            f"> {b_prompt_preview}...\n\n"
            f"💡 使用 `/切换人格 A` 或 `/切换人格 B` 来切换。\n"
            f"💡 切换后**不会清空聊天记录**，两个人格共用记忆。\n"
            f"⚠️ 注意：此操作需要管理员权限。"
        )

    # ------------------------------------------------------------------
    # 消息监听：快捷切换指令 /人格名来了
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_switch_trigger_message(self, event: AstrMessageEvent):
        """
        监听形如 /人格名来了 的消息，作为快捷切换指令。
        只有匹配已配置的两个人格名 + 触发后缀时才会响应，避免正常对话触发。
        """
        # 未配置时不处理
        if not self._persona_a_id and not self._persona_b_id:
            return

        msg = (event.message_str or "").strip()
        if not msg.startswith("/"):
            return

        # 构造正则：/人格名[可选空格]来了
        # 人格名来自配置，且需要转义特殊字符
        target_names = []
        for name in (self._persona_a_id, self._persona_b_id):
            if name:
                target_names.append(re.escape(name))
        if not target_names:
            return

        suffix = re.escape(self._switch_trigger_suffix or "来了")
        pattern = rf"^/({'|'.join(target_names)})\s*{suffix}$"

        match = re.match(pattern, msg)
        if not match:
            return

        # 匹配到的人格名，对应 key
        target_name = match.group(1)
        matched_key = None
        for key, pid in (("a", self._persona_a_id), ("b", self._persona_b_id)):
            if pid == target_name:
                matched_key = key
                break
        if not matched_key:
            return

        # 执行切换
        await self._ensure_persona_data_loaded()
        old_key = self._current_key
        new_key = matched_key

        await self._do_switch(new_key)

        # 发送剧情化切换台词
        async for msg in self._emit_switch_lines(event, old_key, new_key):
            yield msg

        # 阻止事件继续传播，避免被其他插件或普通对话逻辑处理
        event.stop_event()

    # ------------------------------------------------------------------
    # 指令：/人格列表
    # ------------------------------------------------------------------
    @filter.command("人格列表")
    async def list_personas(self, event: AstrMessageEvent):
        """列出当前可用的两个人格及其详情"""
        await self._ensure_persona_data_loaded()

        a_name = self._get_persona_name("a")
        b_name = self._get_persona_name("b")
        current_name = self._get_persona_name(self._current_key)

        a_prompt = self._get_prompt_preview("a")
        b_prompt = self._get_prompt_preview("b")

        yield event.plain_result(
            f"🎭 **可用人格列表**\n\n"
            f"当前激活：**{current_name}**\n\n"
            f"─── 人格 A ───\n"
            f"名称：{a_name}\n"
            f"描述：{a_prompt}...\n\n"
            f"─── 人格 B ───\n"
            f"名称：{b_name}\n"
            f"描述：{b_prompt}...\n\n"
            f"💡 使用 `/切换人格 A` 或 `/切换人格 B` 来切换。"
        )

    # ==================================================================
    # 剧情化切换台词
    # ==================================================================

    async def _emit_switch_lines(
        self, event: AstrMessageEvent, old_key: str, new_key: str
    ):
        """根据配置，发送切换时的退场语和登场语"""
        if self._switch_lines_mode == "none":
            new_name = self._get_persona_name(new_key)
            yield event.plain_result(f"✨ 人格已切换为「{new_name}」（聊天上下文已保留）。")
            return

        # 作者专属模板
        if self._switch_lines_mode == "personalized" and self._enable_author_template:
            # 个性化默认模板（根据人格设定）
            old_persona_id = self._get_persona_id(old_key)
            new_persona_id = self._get_persona_id(new_key)
            old_name = self._get_persona_name(old_key)
            new_name = self._get_persona_name(new_key)

            # 根据人格名称选择个性化的默认模板
            if old_persona_id == self._persona_a_id and new_persona_id == self._persona_b_id:
                # 小澜 -> 小汐
                exit_line = "哼，{name}就先去处理点事，别把我忘了。"
                enter_line = "{next_name}，{name}回来了，你可别以为我会忘记你。"
            elif old_persona_id == self._persona_b_id and new_persona_id == self._persona_a_id:
                # 小汐 -> 小澜
                exit_line = "{name}要休息一下，别吵到我。"
                enter_line = "哼，{name}来啦，别以为我会轻易原谅你。"
            else:
                # 默认通用模板
                exit_line = "{name}先退下了，{next_name}会陪您的。"
                enter_line = "{name}来啦~"

        # 自定义模板
        elif self._switch_lines_mode == "custom":
            old_persona_id = self._get_persona_id(old_key)
            new_persona_id = self._get_persona_id(new_key)
            old_name = self._get_persona_name(old_key)
            new_name = self._get_persona_name(new_key)
            
            exit_line = self._get_custom_line(old_persona_id, "exit_line")
            enter_line = self._get_custom_line(new_persona_id, "enter_line")
            if not exit_line:
                exit_line = "{name}先退下了，{next_name}会陪您的。"
            if not enter_line:
                enter_line = "{name}来啦~"

        # 默认模板
        else:
            old_persona_id = self._get_persona_id(old_key)
            new_persona_id = self._get_persona_id(new_key)
            old_name = self._get_persona_name(old_key)
            new_name = self._get_persona_name(new_key)
            
            exit_line = "{name}先退下了，{next_name}会陪您的。"
            enter_line = "{name}来啦~"

        # 发送退场语
        if exit_line:
            exit_text = self._format_switch_line(
                exit_line, old_name, new_name, old_persona_id, new_persona_id
            )
            yield event.plain_result(exit_text)

        # 发送登场语
        if enter_line:
            enter_text = self._format_switch_line(
                enter_line, new_name, old_name, new_persona_id, old_persona_id
            )
            yield event.plain_result(enter_text)

    def _get_custom_line(self, persona_id: str, line_type: str) -> str | None:
        """从自定义配置中读取某个人格的退场/登场语"""
        if not persona_id or not self._switch_lines:
            return None
        persona_cfg = self._switch_lines.get(persona_id)
        if not isinstance(persona_cfg, dict):
            return None
        return persona_cfg.get(line_type)

    def _format_switch_line(
        self,
        template: str,
        name: str,
        other_name: str,
        persona_id: str,
        other_persona_id: str,
    ) -> str:
        """替换切换台词中的占位符"""
        return (
            template
            .replace("{name}", name)
            .replace("{persona_id}", persona_id)
            .replace("{next_name}", other_name)
            .replace("{prev_name}", other_name)
            .replace("{next_persona_id}", other_persona_id)
            .replace("{prev_persona_id}", other_persona_id)
        )

    def _get_persona_id(self, key: str) -> str:
        """获取人格 key 对应的 persona_id"""
        if key == "a":
            return self._persona_a_id
        return self._persona_b_id

    # ==================================================================
    # 内部方法：人格数据获取
    # ==================================================================

    async def _get_active_persona_data(self) -> dict | None:
        """获取当前激活人格的完整数据（优先从缓存读取）"""
        if self._current_key == "a":
            if self._persona_a_data is not None:
                return self._persona_a_data
            self._persona_a_data = await self._fetch_persona_by_id(self._persona_a_id)
            return self._persona_a_data
        else:
            if self._persona_b_data is not None:
                return self._persona_b_data
            self._persona_b_data = await self._fetch_persona_by_id(self._persona_b_id)
            return self._persona_b_data

    async def _ensure_persona_data_loaded(self) -> None:
        """确保两个人格的数据都已加载到缓存"""
        if self._persona_a_data is None and self._persona_a_id:
            self._persona_a_data = await self._fetch_persona_by_id(self._persona_a_id)
        if self._persona_b_data is None and self._persona_b_id:
            self._persona_b_data = await self._fetch_persona_by_id(self._persona_b_id)

    async def _fetch_persona_by_id(self, persona_id: str) -> dict | None:
        """
        从 AstrBot 的 PersonaManager 中按 ID 读取人格数据。
        兼容多种 API 版本。
        """
        if not persona_id:
            return None

        pm = self.context.persona_manager

        # 策略 1：PersonaManager.get_persona(persona_id) -> Persona 对象
        if hasattr(pm, "get_persona"):
            try:
                result = await pm.get_persona(persona_id)
                if result is not None:
                    parsed = self._parse_persona_result(result, persona_id)
                    if parsed:
                        return parsed
            except Exception:
                pass

        # 策略 2：像字典一样访问（PersonaManager 可能实现了 __getitem__）
        try:
            result = pm[persona_id]
            parsed = self._parse_persona_result(result, persona_id)
            if parsed:
                return parsed
        except (KeyError, TypeError, AttributeError):
            pass

        # 策略 3：get 方法
        if hasattr(pm, "get"):
            try:
                result = await pm.get(persona_id) if callable(pm.get) else pm.get(persona_id)
                parsed = self._parse_persona_result(result, persona_id)
                if parsed:
                    return parsed
            except Exception:
                pass

        # 策略 4：personas_v3 列表（Personality dict，按 name 匹配）
        try:
            personas_v3 = getattr(pm, "personas_v3", None)
            if personas_v3:
                for p in personas_v3:
                    if p.get("name") == persona_id:
                        return {
                            "name": p.get("name", persona_id),
                            "prompt": p.get("prompt", ""),
                            "raw": p,
                        }
        except Exception:
            pass

        # 策略 5：personas 列表（Persona 对象列表，按 persona_id 匹配）
        try:
            personas = getattr(pm, "personas", None)
            if personas:
                for p in personas:
                    if getattr(p, "persona_id", None) == persona_id:
                        return {
                            "name": getattr(p, "persona_id", persona_id),
                            "prompt": getattr(p, "system_prompt", ""),
                            "raw": p,
                        }
        except Exception:
            pass

        logger.warning(f"[DualPersona] 无法读取人格 {persona_id}，API 不匹配")
        return None

    def _parse_persona_result(self, result: object, persona_id: str) -> dict | None:
        """统一解析 Persona / Personality 对象或字典"""
        if result is None:
            return None

        # 字典形式（v3 Personality）
        if isinstance(result, dict):
            return {
                "name": result.get("name", persona_id),
                "prompt": result.get("prompt", ""),
                "raw": result,
            }

        # dataclass / 对象形式（数据库存储的 Persona）
        # 可能字段：system_prompt, prompt, persona_id, name
        prompt = getattr(result, "system_prompt", None) or getattr(result, "prompt", "")
        name = (
            getattr(result, "name", None)
            or getattr(result, "persona_id", None)
            or persona_id
        )

        if prompt:
            return {
                "name": name,
                "prompt": prompt,
                "raw": result,
            }

        return None

    def _get_persona_name(self, key: str) -> str:
        """获取人格显示名称"""
        if key == "a":
            data = self._persona_a_data
            fallback = self._persona_a_id or "未设置"
        else:
            data = self._persona_b_data
            fallback = self._persona_b_id or "未设置"
        return (data or {}).get("name", "") or fallback

    def _get_prompt_preview(self, key: str) -> str:
        """获取人格 prompt 的前 80 字预览"""
        if key == "a":
            data = self._persona_a_data
        else:
            data = self._persona_b_data
        if not data:
            return "(未配置或加载失败)"
        prompt = data.get("prompt", "")
        return prompt[:80] if prompt else "(无描述)"

    # ==================================================================
    # 辅助：人格匹配 & 切换
    # ==================================================================

    def _match_persona(self, name: str) -> str | None:
        """根据用户输入匹配人格 key ('a' 或 'b')，支持按名称模糊匹配"""
        name_lower = name.strip().lower()
        a_name = self._get_persona_name("a").lower()
        b_name = self._get_persona_name("b").lower()

        # 精确匹配 key
        if name_lower in ("a", "人格a"):
            return "a" if self._persona_a_id else None
        if name_lower in ("b", "人格b"):
            return "b" if self._persona_b_id else None

        # 模糊匹配名称
        if a_name and a_name != "未设置" and (
            name_lower == a_name or name_lower in a_name or a_name in name_lower
        ):
            return "a"
        if b_name and b_name != "未设置" and (
            name_lower == b_name or name_lower in b_name or b_name in name_lower
        ):
            return "b"

        return None

    async def _do_switch(self, key: str) -> None:
        """执行人格切换并持久化"""
        self._current_key = key
        await self.put_kv_data("current_persona", key)
        new_name = self._get_persona_name(key)
        logger.info(f"[DualPersona] 人格已切换为: {new_name} (key={key})")

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """检查用户是否为管理员"""
        # 获取用户ID
        sender_id = event.get_sender_id()
        
        # 从配置中获取管理员列表
        admin_uids = self.context.get_config().get("admin_uids", "")
        if admin_uids:
            admins = [uid.strip() for uid in admin_uids.split(",") if uid.strip()]
            if sender_id in admins:
                return True
        
        # 检查是否是全局管理员（如果有的话）
        # 可以添加更多管理员检查逻辑
        return False

    # ==================================================================
    # 生命周期
    # ==================================================================

    async def start(self):
        """插件启动时恢复上次的人格选择"""
        saved = await self.get_kv_data("current_persona", default="a")
        if saved in ("a", "b"):
            self._current_key = saved
            self._last_handoff_key = saved

        logger.info(
            f"[DualPersona] 启动完成 | "
            f"人格 A ID: {self._persona_a_id or '(未设置)'} | "
            f"人格 B ID: {self._persona_b_id or '(未设置)'} | "
            f"当前人格 key: {self._current_key}"
        )

    async def terminate(self):
        """插件卸载时保存当前人格"""
        await self.put_kv_data("current_persona", self._current_key)
        logger.info("[DualPersona] 已保存状态，插件已卸载。")