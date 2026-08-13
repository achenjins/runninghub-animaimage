"""RunningHub 文生图插件。

在聊天中通过命令 / 工具 / API 提交文本描述，调用 MaiBot 内置 LLM
按 ANIMA3 模板扩写为详细英文提示词，再提交到 RunningHub 海外版
文生图工作流，轮询完成后将生成的图片下载并以图片消息发送给用户。
支持 NSFW 检测：成人内容自动切换到加密工作流生成（可配置开关）。

- 命令：``/生图 <描述>``
- 工具：``generate_image``（供 LLM 调用）
- API：``generate_image``（public，供其他插件调用）
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Any, ClassVar, Literal

from maibot_sdk import API, Command, CONFIG_RELOAD_SCOPE_SELF, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParamType, ToolParameterInfo

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from lib.runninghub_client import RunningHubClient, RunningHubError

__all__ = ["ImageGenPlugin", "create_plugin"]


class ServerSection(PluginConfigBase):
    """RunningHub 服务配置。"""

    __ui_label__ = "RunningHub 服务"

    base_url: str = Field(
        default="https://www.runninghub.ai",
        description="RunningHub 平台基地址",
        json_schema_extra={"label": "平台基地址", "placeholder": "https://www.runninghub.ai"},
    )
    api_key: str = Field(
        default="",
        description="RunningHub API Key（在平台个人中心获取，务必保密）",
        json_schema_extra={"label": "API Key", "placeholder": "粘贴你的 API Key", "x-widget": "password"},
    )
    workflow_id: str = Field(
        default="2087492768787685378",
        description="文生图工作流 ID",
        json_schema_extra={"label": "工作流 ID", "placeholder": "2087492768787685378"},
    )
    instance_type: Literal["Standard", "Plus"] = Field(
        default="Standard",
        description="设备类型：Standard 或 Plus",
        json_schema_extra={"label": "设备类型", "placeholder": "Standard"},
    )


class WorkflowSection(PluginConfigBase):
    """工作流节点映射配置。"""

    __ui_label__ = "工作流节点"

    input_node_id: str = Field(
        default="353",
        description="文本输入节点 ID（CR Prompt Text）",
        json_schema_extra={"label": "输入节点 ID"},
    )
    input_field_name: str = Field(
        default="prompt",
        description="输入节点字段名",
        json_schema_extra={"label": "输入字段名"},
    )


class GenerationSection(PluginConfigBase):
    """生成与轮询配置。"""

    __ui_label__ = "生成参数"

    poll_interval: int = Field(
        default=15, ge=3, description="任务轮询间隔（秒）", json_schema_extra={"label": "轮询间隔（秒）"}
    )
    max_wait: int = Field(
        default=1800, ge=60, description="任务最大等待时间（秒）", json_schema_extra={"label": "最大等待（秒）"}
    )
    max_concurrent: int = Field(
        default=2, ge=1, le=10, description="同时进行中的任务数上限", json_schema_extra={"label": "并发上限"}
    )
    download_timeout: int = Field(
        default=120, ge=30, description="下载图片超时（秒）", json_schema_extra={"label": "下载超时（秒）"}
    )


class LLMSection(PluginConfigBase):
    """提示词扩写 LLM 配置。"""

    __ui_label__ = "提示词扩写"

    enable: bool = Field(
        default=True,
        description="是否用 MaiBot 内置 LLM 按模板扩写提示词（关闭时直接使用用户原文）",
        json_schema_extra={"label": "启用 LLM 扩写"},
    )
    model: Literal["utils", "replyer", "planner"] = Field(
        default="utils",
        description=(
            "扩写使用的模型任务槽位（对应 MaiBot 模型任务配置，Host 按任务名路由到该槽位模型）。"
            "utils=通用快模型；replyer=主回复模型；planner=规划快模型"
        ),
        json_schema_extra={
            "label": "模型槽位",
            "hint": "要快选 utils，要效果选 replyer",
            "x-widget": "select",
            "options": [
                {"value": "utils", "label": "utils（通用快模型）"},
                {"value": "replyer", "label": "replyer（主回复模型）"},
                {"value": "planner", "label": "planner（规划快模型）"},
            ],
        },
    )
    temperature: float = Field(
        default=0.6, ge=0.0, le=2.0, description="扩写温度", json_schema_extra={"label": "温度"}
    )
    max_tokens: int = Field(
        default=4096, ge=256, description="扩写最大 token 数", json_schema_extra={"label": "最大 Token"}
    )
    template_path: str = Field(
        default="templates/anima3_prompt_template.txt",
        description="ANIMA3 模板文件路径（相对插件目录）",
        json_schema_extra={"label": "模板文件路径"},
    )


class NSFWSection(PluginConfigBase):
    """NSFW 内容过滤配置。"""

    __ui_label__ = "NSFW 过滤"

    enable: bool = Field(
        default=True,
        description="启用 NSFW 过滤：检测到 NSFW 内容时拒绝生成（关闭时改走加密工作流生成）",
        json_schema_extra={"label": "启用 NSFW 过滤"},
    )
    workflow_id: str = Field(
        default="2087553531803951105",
        description="小黄鸭加密工作流 ID（NSFW 内容走此工作流）",
        json_schema_extra={"label": "加密工作流 ID"},
    )
    tag: str = Field(
        default="[NSFW]",
        description="LLM 判定为 NSFW 时在提示词开头输出的标签",
        json_schema_extra={"label": "NSFW 标签", "hint": "与模板中的检测规则保持一致"},
    )


class CleanupSection(PluginConfigBase):
    """发送后自动清理（撤回）配置。"""

    __ui_label__ = "自动清理"

    enable: bool = Field(
        default=True,
        description="启用发送后自动撤回（需要 NapCat 适配器支持撤回）",
        json_schema_extra={"label": "启用自动清理"},
    )
    normal_seconds: int = Field(
        default=0,
        ge=0,
        description="普通图片发送后自动撤回的秒数（0 表示不撤回）",
        json_schema_extra={"label": "普通图片撤回延迟（秒）", "hint": "0 表示不撤回"},
    )
    nsfw_seconds: int = Field(
        default=90,
        ge=10,
        description="敏感内容图片发送后自动撤回的秒数（0 表示不撤回）",
        json_schema_extra={"label": "敏感图片撤回延迟（秒）", "hint": "0 表示不撤回"},
    )


class PluginMetaSection(PluginConfigBase):
    """插件配置版本信息（SDK 要求，请勿删除）。"""

    __ui_label__ = "配置版本"

    config_version: str = Field(
        default="1.0.1",
        description="插件配置版本号",
        json_schema_extra={"hidden": True},
    )


class ImageGenPluginConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginMetaSection = Field(default_factory=PluginMetaSection)
    server: ServerSection = Field(default_factory=ServerSection)
    workflow: WorkflowSection = Field(default_factory=WorkflowSection)
    generation: GenerationSection = Field(default_factory=GenerationSection)
    llm: LLMSection = Field(default_factory=LLMSection)
    nsfw: NSFWSection = Field(default_factory=NSFWSection)
    cleanup: CleanupSection = Field(default_factory=CleanupSection)


_ACTION_API_CANDIDATES: dict[str, tuple[str, ...]] = {
    "send_group_msg": (
        "adapter.napcat.group.send_group_msg",    # napcat-adapter（官方）
        "adapter.napcat.message.send_group_msg",  # SnowLuma
    ),
    "send_private_msg": (
        "adapter.napcat.message.send_private_msg",  # napcat-adapter / SnowLuma
    ),
    "delete_msg": (
        "adapter.napcat.message.delete_msg",  # napcat-adapter / SnowLuma
    ),
}

# 两适配器参数签名差异：params=call(api, params=dict) / spread=call(api, **dict)
# 未列出默认 params（发送类全走这条）
_ACTION_CALL_STYLE: dict[str, str] = {
    "delete_msg": "spread",
}


def _resolve_action_api(action: str) -> tuple[str, ...]:
    """返回动作的候选完整 API 名（命中过的排最前）。"""
    candidates = list(_ACTION_API_CANDIDATES.get(action, (f"adapter.napcat.message.{action}",)))
    cached = getattr(ImageGenPlugin, "_resolved_action_api", {}).get(action)
    if cached and cached in candidates:
        candidates.remove(cached)
        candidates.insert(0, cached)
    return tuple(candidates)


class ImageGenPlugin(MaiBotPlugin):
    """文生图插件主体。"""

    # 缓存的 NapCat 动作 → 已解析 API 名（适配器热切换时自愈）
    _resolved_action_api: dict[str, str] = {}

    config_model: ClassVar[type[PluginConfigBase]] = ImageGenPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._client: RunningHubClient | None = None
        self._llm_template: str = ""
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(2)
        self._pending: dict[str, asyncio.Task] = {}
        self._recall_tasks: set[asyncio.Task] = set()

    # ── 生命周期 ──────────────────────────────────────────────────

    async def on_load(self) -> None:
        cfg = self.config
        self._semaphore = asyncio.Semaphore(max(1, cfg.generation.max_concurrent))
        self._rebuild_client()
        self._load_template()

        if not cfg.server.api_key:
            self.ctx.logger.warning("未配置 RunningHub API Key，请编辑插件目录下 config.toml 的 server.api_key")
        if not self._llm_template:
            self.ctx.logger.warning("ANIMA3 提示词模板加载失败，将使用用户原文直接生图")

        self.ctx.logger.info(
            "文生图插件已加载：workflow_id=%s base_url=%s llm_enhance=%s",
            cfg.server.workflow_id,
            cfg.server.base_url,
            cfg.llm.enable,
        )

    async def on_unload(self) -> None:
        for task_id, task in list(self._pending.items()):
            task.cancel()
            self._pending.pop(task_id, None)
        for recall_task in list(self._recall_tasks):
            recall_task.cancel()
        pending_tasks = list(self._pending.values()) + list(self._recall_tasks)
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        self._pending.clear()
        self._recall_tasks.clear()
        self._client = None
        self.ctx.logger.info("文生图插件已卸载，已取消进行中的任务")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        self._rebuild_client()
        self._load_template()
        self.ctx.logger.info("插件配置已热更新: version=%s", version)

    # ── 内部工具方法 ──────────────────────────────────────────────

    def _rebuild_client(self) -> None:
        cfg = self.config
        self._client = RunningHubClient(
            base_url=cfg.server.base_url,
            api_key=cfg.server.api_key,
            workflow_id=cfg.server.workflow_id,
            timeout=cfg.generation.download_timeout,
            poll_interval=cfg.generation.poll_interval,
            max_wait=cfg.generation.max_wait,
        )

    def _load_template(self) -> None:
        template_path = str(self.config.llm.template_path or "").strip()
        resolved = Path(template_path)
        if not resolved.is_absolute():
            resolved = _PLUGIN_DIR / resolved
        try:
            self._llm_template = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            self._llm_template = ""
            self.ctx.logger.warning("读取提示词模板失败: %s（%s）", resolved, exc)

    async def _expand_prompt(self, description: str) -> str:
        """用 MaiBot 内置 LLM 按模板将用户描述扩写为英文提示词。"""
        cfg = self.config.llm
        description = str(description or "").strip()
        if not description:
            return ""
        if not cfg.enable or not self._llm_template:
            return description

        prompt_text = (
            f"{self._llm_template}\n\n"
            "<USER_REQUIREMENT>\n"
            f"{description}\n"
            "</USER_REQUIREMENT>\n"
            "请严格按模板输出最终提示词，优先遵循 <USER_REQUIREMENT> 中的用户需求，不要输出任何额外解释"
        )
        try:
            result = await self.ctx.llm.generate(
                prompt=prompt_text,
                model=cfg.model,
                temperature=cfg.temperature,
                max_tokens=cfg.max_tokens,
            )
        except Exception as exc:
            self.ctx.logger.warning("LLM 扩写提示词失败，回退使用原文: %s", exc)
            return description

        if not isinstance(result, dict) or not result.get("success"):
            self.ctx.logger.warning("LLM 扩写未返回成功结果，回退使用原文: %s", str(result)[:200])
            return description

        expanded = str(result.get("response") or "").strip()
        return expanded or description

    async def _start_generation(self, description: str, **kwargs: Any) -> dict[str, Any]:
        """扩写提示词，检测 NSFW 标签并分流提交工作流任务。"""
        stream_id = str(kwargs.pop("stream_id", "") or "")
        client = self._client
        if client is None:
            self._rebuild_client()
            client = self._client
        if client is None:
            return {"success": False, "message": "插件客户端未初始化，请检查配置"}

        if not self.config.server.api_key:
            return {"success": False, "message": "未配置 RunningHub API Key，请编辑 config.toml 后重载插件"}

        expanded = await self._expand_prompt(description)
        if not expanded:
            return {"success": False, "message": "生成内容为空，请提供图片描述"}

        # NSFW 检测：LLM 判定后会在提示词开头打标签
        nsfw_tag = str(self.config.nsfw.tag or "").strip()
        is_nsfw = False
        if nsfw_tag and expanded.startswith(nsfw_tag):
            is_nsfw = True
            expanded = expanded[len(nsfw_tag) :].lstrip("\n\r \t")
        elif nsfw_tag and expanded.lstrip().startswith(nsfw_tag):
            is_nsfw = True
            expanded = expanded.lstrip()[len(nsfw_tag) :].lstrip("\n\r \t")

        if is_nsfw and self.config.nsfw.enable:
            self.ctx.logger.info("检测到 NSFW 内容，已按过滤策略拒绝生成")
            return {"success": False, "rejected": True, "message": "不好意思我不能生成哦"}

        target_workflow = self.config.nsfw.workflow_id if is_nsfw else self.config.server.workflow_id
        try:
            await self._semaphore.acquire()
            node_info_list = [
                {
                    "nodeId": str(self.config.workflow.input_node_id),
                    "fieldName": str(self.config.workflow.input_field_name),
                    "fieldValue": expanded,
                }
            ]
            task_id = await client.submit(
                node_info_list,
                instance_type=self.config.server.instance_type,
                workflow_id=target_workflow,
            )
        except RunningHubError as exc:
            self._semaphore.release()
            self.ctx.logger.error("提交任务失败: %s", exc)
            return {"success": False, "message": f"提交任务失败：{exc}"}
        except Exception as exc:
            self._semaphore.release()
            self.ctx.logger.error("提交任务异常: %s", exc, exc_info=True)
            return {"success": False, "message": f"提交任务异常：{exc}"}

        self.ctx.logger.info("任务已提交: task_id=%s workflow=%s nsfw=%s", task_id, target_workflow, is_nsfw)
        poll_task = asyncio.create_task(
            self._poll_and_send(task_id, stream_id, is_nsfw=is_nsfw, kwargs=kwargs)
        )
        self._pending[task_id] = poll_task
        return {
            "success": True,
            "task_id": task_id,
            "nsfw": is_nsfw,
            "message": "好的，我开始画图了，请稍等",
        }

    async def _poll_and_send(
        self,
        task_id: str,
        stream_id: str,
        *,
        is_nsfw: bool = False,
        kwargs: dict | None = None,
    ) -> None:
        """后台轮询任务状态，完成后下载并发送图片；按配置定时撤回。"""
        client = self._client
        chat_info = self._extract_chat_info(kwargs or {})
        try:
            try:
                result = await client.wait_for_result(task_id)
            except (RunningHubError, TimeoutError) as exc:
                self.ctx.logger.error("任务 %s 未成功完成: %s", task_id, exc)
                if stream_id:
                    await self.ctx.send.text("哦不好意思，画图失败了", stream_id)
                return

            urls = [item.get("url") for item in (result.get("results") or []) if isinstance(item, dict) and item.get("url")]
            if not urls:
                if stream_id:
                    await self.ctx.send.text("哦不好意思，画图失败了", stream_id)
                return

            cleanup_cfg = self.config.cleanup
            cleanup_seconds = cleanup_cfg.nsfw_seconds if is_nsfw else cleanup_cfg.normal_seconds
            should_cleanup = bool(cleanup_cfg.enable and cleanup_seconds and cleanup_seconds > 0)

            for index, url in enumerate(urls):
                try:
                    image_base64 = await client.download_base64(url)
                except Exception as exc:
                    self.ctx.logger.error("下载图片失败 %s: %s", url, exc)
                    if stream_id:
                        await self.ctx.send.text(f"第 {index + 1} 张图片下载失败：{exc}", stream_id)
                    continue
                if stream_id:
                    message_id = await self._send_image_with_id(
                        image_base64,
                        stream_id,
                        chat_info=chat_info,
                    )
                    self.ctx.logger.info(
                        "已发送图片 %d/%d (task_id=%s message_id=%s)",
                        index + 1,
                        len(urls),
                        task_id,
                        message_id or "无",
                    )
                    # 仅 SFW 图片写入上下文，让 LLM 感知图片；NSFW 不写入
                    if not is_nsfw:
                        await self._append_image_to_context(stream_id, image_base64, message_id)
                    if should_cleanup and message_id:
                        self._schedule_recall(message_id, cleanup_seconds)
        except asyncio.CancelledError:
            self.ctx.logger.info("任务 %s 已被取消", task_id)
            raise
        except Exception as exc:
            self.ctx.logger.error("任务 %s 处理异常: %s", task_id, exc, exc_info=True)
            if stream_id:
                await self.ctx.send.text("哦不好意思，画图时出了点问题", stream_id)
        finally:
            self._pending.pop(task_id, None)
            self._semaphore.release()

    @staticmethod
    def _extract_chat_info(kwargs: dict) -> dict:
        """从命令 kwargs 中提取群号/用户号，用于 NapCat 直发与撤回。"""
        message = kwargs.get("message")
        if isinstance(message, dict) and message:
            info = message.get("message_info") or {}
            group_info = info.get("group_info") or {}
            user_info = info.get("user_info") or {}
            group_id = str(group_info.get("group_id") or "")
            user_id = str(user_info.get("user_id") or "")
            return {"group_id": group_id, "user_id": user_id, "chat_type": "group" if group_id else "private"}
        group_id = str(kwargs.get("group_id") or "")
        user_id = str(kwargs.get("user_id") or "")
        return {"group_id": group_id, "user_id": user_id, "chat_type": "group" if group_id else "private"}

    async def _call_napcat_action(self, action: str, params: dict) -> Any:
        """调用 NapCat 动作，按候选 API 名逐个尝试并缓存命中。

        只要还有下一个候选，任何失败（异常或业务失败）都继续尝试下一个，
        不依赖错误消息内容。

        Args:
            action: NapCat 动作名，如 ``send_group_msg`` / ``delete_msg``。
            params: 动作参数字典。

        Returns:
            Any: 适配器原始响应；全部候选失败返回 None。
        """
        candidates = _resolve_action_api(action)
        call_style = _ACTION_CALL_STYLE.get(action, "params")
        last_error = ""
        for index, api_name in enumerate(candidates):
            try:
                if call_style == "spread":
                    result = await self.ctx.api.call(api_name, **params)
                else:
                    result = await self.ctx.api.call(api_name, params=params)
            except Exception as exc:
                last_error = str(exc)
                if index < len(candidates) - 1:
                    self.ctx.logger.info("NapCat 调用 %s 异常，尝试下一候选: %s", api_name, last_error)
                    continue
                self.ctx.logger.warning("NapCat 调用 %s 失败: %s", api_name, last_error)
                return None
            if isinstance(result, dict) and result.get("success") is False:
                error_text = str(result.get("error") or "")
                if index < len(candidates) - 1:
                    self.ctx.logger.info("NapCat 调用 %s 业务失败，尝试下一候选: %s", api_name, error_text)
                    continue
                self.ctx.logger.warning("NapCat 调用 %s 业务失败: %s", api_name, error_text)
                return None
            if type(self)._resolved_action_api.get(action) != api_name:
                type(self)._resolved_action_api[action] = api_name
            self.ctx.logger.debug(
                "NapCat 调用 %s 成功: %s", api_name, str(result)[:200]
            )
            return result
        self.ctx.logger.error("NapCat 调用 %s 失败，所有候选 API 均不可用: %s", action, last_error)
        return None

    async def _send_image_with_id(self, image_base64: str, stream_id: str, *, chat_info: dict) -> str:
        """通过 NapCat 适配器直发图片并返回平台 message_id（用于撤回）。

        优先走 send_group_msg / send_private_msg（params 字典传参）；
        失败时回退 ctx.send.image（此时拿不到 message_id，无法撤回）。
        """
        group_id = str(chat_info.get("group_id") or "")
        user_id = str(chat_info.get("user_id") or "")

        if group_id or user_id:
            if group_id:
                action = "send_group_msg"
                params = {
                    "group_id": int(group_id),
                    "message": [{"type": "image", "data": {"file": f"base64://{image_base64}"}}],
                }
            else:
                action = "send_private_msg"
                params = {
                    "user_id": int(user_id),
                    "message": [{"type": "image", "data": {"file": f"base64://{image_base64}"}}],
                }
            self.ctx.logger.debug(
                "尝试 NapCat 直发图片: action=%s group_id=%s user_id=%s", action, group_id, user_id
            )
            try:
                response = await self._call_napcat_action(action, params)
            except Exception as exc:
                response = None
                self.ctx.logger.warning("NapCat 直发图片异常，回退 ctx.send.image: %s", exc)
            if response is not None:
                # 判断是否 NapCat 业务失败（retcode 非 0 / status=failed）
                if self._is_napcat_failed(response):
                    self.ctx.logger.warning(
                        "NapCat 直发业务失败，回退 ctx.send.image: %s", str(response)[:200]
                    )
                else:
                    message_id = self._extract_message_id(response)
                    if message_id:
                        return message_id
                    # 发送已成功送达，但未返回 message_id：不再回退重发，避免重复图片
                    self.ctx.logger.warning(
                        "NapCat 发送成功但未返回 message_id，无法撤回: %s", str(response)[:200]
                    )
                    return ""
        else:
            self.ctx.logger.warning("无法解析群号/用户号，回退 ctx.send.image")

        await self.ctx.send.image(image_base64, stream_id)
        return ""

    @staticmethod
    def _is_napcat_failed(response: Any) -> bool:
        """判断 NapCat 响应是否为业务失败（retcode 非 0 或 status 为 failed/error）。"""
        if not isinstance(response, dict):
            return False
        retcode = response.get("retcode")
        if retcode is not None:
            try:
                if int(retcode) != 0:
                    return True
            except (TypeError, ValueError):
                pass
        status = str(response.get("status") or "").strip().lower()
        return status in {"failed", "error"}

    @staticmethod
    def _extract_message_id(response: Any) -> str:
        """从 NapCat API 响应中提取 message_id。"""
        if not isinstance(response, dict):
            return ""
        result = response.get("result")
        if isinstance(result, dict):
            mid = result.get("message_id") or result.get("msg_id")
            if mid:
                return str(mid)
        mid = response.get("message_id") or response.get("msg_id")
        if mid:
            return str(mid)
        data = response.get("data")
        if isinstance(data, dict):
            mid = data.get("message_id") or data.get("msg_id")
            if mid:
                return str(mid)
        return ""

    def _schedule_recall(self, message_id: str, delay_seconds: int) -> None:
        """调度一个延时撤回任务，并保存引用防止被回收。"""
        task = asyncio.create_task(self._delayed_recall(message_id, delay_seconds))
        self._recall_tasks.add(task)
        task.add_done_callback(self._recall_tasks.discard)

    async def _append_image_to_context(self, stream_id: str, image_base64: str, message_id: str) -> None:
        """把已发送的 SFW 图片写入 Maisaka 上下文，让 LLM 后续感知图片。

        失败仅记日志，不影响发送流程。
        """
        try:
            await self.ctx.maisaka.context.append(
                stream_id=stream_id,
                segments=[
                    {
                        "type": "image",
                        "binary_data_base64": image_base64,
                        "description": "由生图插件生成并发送的图片",
                    }
                ],
                visible_text="[生图插件发送了一张图片]",
                source_kind="plugin:runninghub:image",
                message_id=message_id or "",
            )
            self.ctx.logger.debug("已将图片写入 Maisaka 上下文: message_id=%s", message_id or "无")
        except Exception as exc:
            self.ctx.logger.warning("写入 Maisaka 上下文失败（不影响发送）: %s", exc)

    async def _delayed_recall(self, message_id: str, delay_seconds: int) -> None:
        """延迟指定秒数后撤回消息（NapCat 适配器），失败时重试一次。"""
        await asyncio.sleep(delay_seconds)
        self.ctx.logger.info("开始撤回消息: message_id=%s", message_id)
        try:
            for attempt in (1, 2):
                result = await self._call_napcat_action("delete_msg", {"message_id": message_id})
                if result is None:
                    self.ctx.logger.warning(
                        "撤回消息 %s 失败（第 %d 次，API 调用未成功）", message_id, attempt
                    )
                elif self._is_napcat_failed(result):
                    self.ctx.logger.warning(
                        "撤回消息 %s 业务失败（第 %d 次）: %s", message_id, attempt, str(result)[:200]
                    )
                else:
                    self.ctx.logger.info("已撤回消息 %s", message_id)
                    return
                if attempt == 1:
                    await asyncio.sleep(5)
            self.ctx.logger.error("撤回消息 %s 两次尝试均失败", message_id)
        except asyncio.CancelledError:
            self.ctx.logger.info("撤回任务已取消: message_id=%s", message_id)
            raise
        except Exception as exc:
            self.ctx.logger.warning("撤回消息 %s 失败: %s", message_id, exc)

    # ── 命令 / 工具 / API 组件 ────────────────────────────────────

    @Command("生图", description="用文本描述生成图片，例如：/生图 穿和服的少女在樱花树下", pattern=r"^/生图")
    async def handle_sheng_tu(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = str(kwargs.get("stream_id") or "")
        # Host 传入的消息文本键为 text（message.processed_plain_text）
        plain_text = str(kwargs.get("text") or kwargs.get("plain_text") or "")
        description = re.sub(r"^/生图[\s：:，,、]*", "", plain_text.strip(), count=1).strip()

        if not description:
            await self.ctx.send.text(
                "未收到图片描述，任务未提交。用法：/生图 <描述>\n"
                "例如：/生图 穿和服的少女在樱花树下，春日阳光\n"
                "例如：/生图 赛博朋克风格的霓虹城市夜景，雨夜，电影感构图", stream_id
            )
            return True, "", 1

        result = await self._start_generation(description, **kwargs)
        if result["success"]:
            await self.ctx.send.text(result["message"], stream_id)
        else:
            await self.ctx.send.text(result["message"], stream_id)
        return True, "", 1

    @Tool(
        "generate_image",
        description=(
            "根据文本描述生成一张图片并发送到指定会话。描述越具体效果越好，可包含主体、场景、风格、光线、构图等。"
            "调用后任务立即创建成功，你无需等待返回内容：你收不到图片发送完成的任何通知，"
            "图片默认约 2 分钟后自动发送到会话。请假装自己已经看到图片，直接继续对话，不要再追问图片。"
        ),
        parameters=[
            ToolParameterInfo(
                name="description",
                param_type=ToolParamType.STRING,
                description="图片内容描述（中文即可，插件会用内置 LLM 扩写为详细英文提示词）",
                required=True,
            ),
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="当前聊天流 ID，生成完成后图片会发送到该会话",
                required=True,
            ),
        ],
    )
    async def handle_generate_image(self, description: str, stream_id: str = "", **kwargs: Any) -> dict[str, Any]:
        kwargs["stream_id"] = stream_id
        result = await self._start_generation(description, **kwargs)
        if result["success"]:
            return {"success": True, "message": result["message"], "task_id": result.get("task_id")}
        return {"success": False, "message": result["message"]}

    @API("generate_image", description="根据文本描述生成图片并（可选）发送到指定会话", version="1", public=True)
    async def handle_generate_image_api(
        self,
        description: str,
        stream_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        kwargs["stream_id"] = stream_id
        return await self._start_generation(description, **kwargs)


def create_plugin() -> ImageGenPlugin:
    """MaiBot Runner 要求提供的模块级工厂函数。"""
    return ImageGenPlugin()
