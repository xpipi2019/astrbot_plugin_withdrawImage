# astrbot_plugin_withdrawImage

群聊图片/QQ 表情自动撤回插件（AstrBot）。

在 OneBot v11（`aiocqhttp`）群聊中，按你设置的规则自动撤回指定图片或 QQ 表情（Face）。

## 功能简介

- 按表情 ID 屏蔽：命中后自动撤回
- 按图片标识子串屏蔽：匹配 `file` / `url` / `file_unique`
- 每个群独立规则（SQLite 持久化）
- 支持规则列表、删除、清空
- 支持 `list` 预览开关（是否发送表情/图片预览）

## 适用平台

- `aiocqhttp`（OneBot v11）

## 命令用法

所有命令前缀为：`/imgblk`

- `face <id>`
  - 添加 QQ 表情 ID 规则
  - 示例：`/imgblk face 177`

- `img <片段>`
  - 添加图片匹配规则（子串匹配，不区分大小写）
  - 示例：`/imgblk img image.example.com/abc`

- `img`（无参数）
  - 先引用一条带图消息，再发送 `/imgblk img`
  - 插件会从引用消息自动提取图片标识入库

- `list`
  - 查看当前群规则

- `del <序号>`
  - 按 `list` 的序号删除规则
  - 示例：`/imgblk del 2`

- `clear`
  - 清空当前群规则

- `preview on|off`
  - 开关 `list` 时的预览消息发送
  - 示例：`/imgblk preview off`

## 权限说明

- 仅在群聊中生效
- 仅群主、群管理员或 AstrBot 超级用户可管理规则
- 自动撤回依赖协议端 `delete_msg` 能力，机器人通常需要具备撤回他人消息权限

## 配置项

`_conf_schema.json` 当前提供：

- `enable_list_preview`（bool，默认 `true`）
  - 是否在 `/imgblk list` 后发送表情/图片预览消息

## 存储说明

- 规则使用 SQLite 存储
- 数据文件位于 AstrBot 的 `plugin_data` 目录下（插件子目录内）

## 元数据

- 名称：`astrbot_plugin_withdraw_image`
- 版本：`v1.0.0`
- 仓库：[https://github.com/xpipi2019/astrbot_plugin_withdrawImage](https://github.com/xpipi2019/astrbot_plugin_withdrawImage)