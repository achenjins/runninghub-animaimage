# RunningHub 文生图插件

MaiBot 第三方插件：输入中文描述，调用 MaiBot 内置 LLM 按 ANIMA3 模板扩写为英文提示词，提交 RunningHub 文生图工作流，生成后自动把动漫风格图片发送到会话。

## 安装

```bash
cd <MaiBot目录>/plugins
git clone https://github.com/achenjins/runninghub-animaimage.git image-plugin
pip install -r image-plugin/requirements.txt
```

重启 MaiBot（或 WebUI 热重载）即自动加载。

## 关键配置

在 MaiBot WebUI 插件配置页填写，或编辑 `plugins/image-plugin/config.toml`。

**必填：**

```toml
[server]
api_key = "你的 RunningHub API Key"   # 必填，平台个人中心获取
```

**常用：**

| 配置 | 默认值 | 说明 |
|------|--------|------|
| `server.instance_type` | `Standard` | 设备类型：Standard / Plus |
| `generation.poll_interval` | `15` | 轮询间隔（秒） |
| `llm.model` | `utils` | 扩写模型槽位：utils / replyer / planner |
| `nsfw.enable` | `true` | 敏感内容过滤开关，默认开启 |
| `cleanup.enable` | `true` | 发送后自动撤回开关 |
| `cleanup.normal_seconds` | `0` | 普通图片撤回延迟（秒），0 表示不撤回 |
| `cleanup.nsfw_seconds` | `90` | 敏感图片撤回延迟（秒），0 表示不撤回 |

其余字段均有默认值，无需改动。完整配置项见插件内 `config.example.toml`。

## 使用

- **命令**：`/生图 穿和服的少女在樱花树下，春日阳光`
- **LLM 工具**：对话中模型自动调用 `generate_image`
- **其他插件**：`ctx.api.call("github.achenjins.runninghub-animaimage.generate_image", description=..., stream_id=...)`

生成约需 1-3 分钟，完成后自动发送；失败或超时会返回原因。

## 敏感内容过滤

- 插件内置内容判定：对特定类型请求会在提示词开头打上标记
- 过滤开启（默认）：检测到标记直接婉拒
- 过滤关闭：剔除标记后继续生成，结果以常规方式发送

## 自动清理

- 图片通过 NapCat 适配器直发（`send_group_msg` / `send_private_msg`），获取平台消息 ID 后按配置延迟自动撤回（`delete_msg`）
- 普通图片默认不撤回，敏感图片默认 90 秒后撤回
- `cleanup.enable = false` 或对应秒数设为 0 可关闭
- 若 NapCat 直发失败会回退 `ctx.send.image`（此时无法撤回）

## 指令拦截

- `/生图` 指令由插件处理，不会进入 AI 正常对话流程

## 测试

```bash
cd plugins/image-plugin
pip install maibot-plugin-sdk requests
python -m unittest discover -s tests -v
```

## 许可证

MIT
