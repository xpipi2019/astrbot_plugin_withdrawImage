# 🖼️ 群聊图片表情自动撤回

<div align="center">

[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-orange.svg?style=flat-square)](https://github.com/AstrBotDevs/AstrBot)
[![版本](https://img.shields.io/badge/版本-v1.1.0-brightgreen.svg?style=flat-square)](#)
[![状态](https://img.shields.io/badge/状态-可用-success.svg?style=flat-square)](#)

*一款轻量化的，具有在群聊中按规则自动撤回指定图片、QQ 表情（Face）与 Emoji 字符的 AstrBot 插件，支持分群独立管理与持久化存储。*

</div>

<div align="center">

[![XPIPI](https://count.getloli.com/@XPIPI?name=XPIPI&theme=original-new&padding=7&offset=0&align=center&scale=1&pixelated=1&darkmode=auto)](https://github.com/xpipi2019/astrbot_plugin_withdrawImage)

</div>

<p align="center">
  <a href="#-简介">简介</a> •
  <a href="#-特性">特性</a> •
  <a href="#-快速开始">快速开始</a> •
  <a href="#-命令说明">命令说明</a> •
  <a href="#-项目结构">项目结构</a>
</p>

---

## 📝 简介

你是否苦恼于群友发送令人不适的图片或表情包，使用本插件可以帮助你！
本插件用于 AstrBot 群聊场景：当消息中出现被屏蔽的图片、QQ 表情或 Emoji 字符时，自动执行撤回。  
规则按群独立维护，使用 SQLite 持久化，重启后不会丢失。

> 💡 适合用于群聊内容治理、表情管控和图片过滤等场景。

## ✨ 特性

- 🧩 **分群独立规则**：每个群有自己的屏蔽列表，互不影响
- 🖼️ **图片 file 优先匹配**：优先以 `file` 精确命中，同时保留 `url` / `file_unique` 子串匹配
- 😀 **表情 ID 屏蔽**：按 QQ Face `id` 精确命中
- 😄 **Emoji 字符屏蔽**：按可识别的 Emoji 字符命中（如 `😀`）
- 💬 **引用自动入库**：`/imgblk img` 仅支持引用带图消息自动提取规则
- 🖼️ **本地预览图落盘**：引用添加时会保存重采样预览图（最长边 < 200），`list` 时随规则一并发送
- 🧹 **删除同步清理**：`/imgblk del` 与 `/imgblk clear` 会同步删除对应本地预览图
- 🧱 **SQLite 持久化**：规则自动落盘，重启不丢
- 🔐 **权限控制**：仅群主/群管理员/AstrBot 超级用户可管理
- 📋 **列表分页展示**：`/imgblk list` 超过 10 条时，每次仅显示前 10 条
- ⚡ **高频路径优化**：按群规则缓存 + 规则预归一化，降低每条消息查库成本

## 🚀 快速开始

### 环境要求

- AstrBot
- OneBot v11 适配（`aiocqhttp`）
- 协议端支持 `delete_msg`（通常机器人需具备撤回他人消息权限）

### 安装与启用

1. 将插件放入 AstrBot 插件目录（或通过你的插件管理方式安装）
2. 重启/重载 AstrBot
3. 在群聊中使用 `/imgblk` 系列命令进行配置

## 📖 命令说明

命令前缀：`/imgblk`

| 命令 | 说明 | 示例 |
|------|------|------|
| `face <id>` | 添加 QQ 表情 ID 屏蔽规则 | `/imgblk face 177` |
| `emoji <字符>` | 添加 Emoji 字符屏蔽规则 | `/imgblk emoji 😀` |
| `img`（无参数） | 引用一条带图消息并自动提取规则 | 引用后发送 `/imgblk img` |
| `list [页码]` | 查看当前群规则列表（分页） | `/imgblk list`、`/imgblk list 2` |
| `del <序号>` | 按列表序号删除规则 | `/imgblk del 2` |
| `clear` | 清空当前群全部规则 | `/imgblk clear` |
## 🧭 兼容平台

- `aiocqhttp`（OneBot v11）

## 📁 项目结构

```text
astrbot_plugin_withdrawImage/
├── README.md          # 项目说明文档
├── main.py            # 插件主逻辑（事件监听、命令处理、撤回逻辑）
└── metadata.yaml      # 插件元数据
```

## 💾 数据存储

- 使用 SQLite 存储规则
- 数据目录通过 `StarTools.get_data_dir()` 获取（遵循 AstrBot 框架约定）
- 图片预览图保存在插件数据目录下的 `IMG_ASSET` 文件夹

## 🔗 相关链接

- 插件仓库：[astrbot_plugin_withdrawImage](https://github.com/xpipi2019/astrbot_plugin_withdrawImage)
- AstrBot 仓库：[AstrBot](https://github.com/AstrBotDevs/AstrBot)

***LOVE FROM ♥️ XPIPI***