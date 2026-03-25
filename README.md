# CloudFlare Tunnel Monitor (as AstrBot Plugin)
一个用于定时轮询监测 [CloudFlare Tunnel](https://developers.cloudflare.com/tunnel/) 状态的 AstrBot 插件。

![Moe Counter](https://count.getloli.com/@gh-sctop_astrbot_cft)

## 功能 & 特色
- 自动轮询监测 (基于 asyncio)
- 使用 CloudFlare 官方 Python SDK
- **纯人工石山代码，不一定比Vibe Coding更稳定，但确实是我自己写来玩的**
  - 至于为什么是人工手写，主要是想学习 Python 异步库的相关使用（我们 JS 的 Promise 真的是太好用辣），以及对抗自己的内耗（想证明一下自己），同时也的确是有一点需求（不想买付费内网穿透

## 使用方法
1. 在[此处](https://dash.cloudflare.com/profile/api-tokens)申请你的 CF API Token
2. 根据[此文档](https://developers.cloudflare.com/fundamentals/account/find-account-and-zone-ids/)在你的[CF 账号面板首页](http://dash.cloudflare.com/?to=/:account/home)找到你的 Account ID
3. 下载本插件
4. 配置 API Token 和 Account ID
5. 保存并重启插件，应该能用了

### 注意事项
CloudFlare API 有多个全局速率限制（Rate Limit）：

- 每个Account(账号)/User(用户)API: 1200req/5min (avg. 4req/s)
- 每个IP: 200req/s

当触发上述任一速率限制时，API调用即遭限制；限制于触发后5分钟后自动解除。

另请参见[Rate limits · Cloudflare Fundamentals docs](https://developers.cloudflare.com/fundamentals/api/reference/limits/)

## 命令
本插件使用 AstrBot 的 **命令组 (Command Group)** 组合命令。

本插件命令组名称为 **`cft`**。

```text
插件 astrbot_plugin_cloudflare_tunnel_monitor: 参数不足。cft 指令组下有如下指令，请参考：
cft
├── on (target_umo(NoneType)=None): 启用本 umo 聊天，或指定一个 umo 聊天，的【主动】推送功能
├── off (target_umo(NoneType)=None): 关闭本 umo 聊天，或指定一个 umo 聊天，的【主动】推送功能
├── add (name(str)): 向当前对话中新增一个要主动监控的 Tunnel
├── remove (name(str)): 移除当前对话中一个主动监控的 Tunnel
├── list (无参数指令): 列出当前对话中所有主动监测的 Tunnel
├── list_all_tunnels (无参数指令): 列出所有正在监控的 Tunnels (不止当前对话)
├── list_all_tunnels_api (无参数指令): 列出整个 API Token/Account ID 下面都可以用于监测的 Tunnels
├── clear (无参数指令): 将当前聊天的所有tunnel监听任务给爆了
├── force_update (无参数指令): 跳过轮询时间，直接强制更新
├── reset (无参数指令): 重置所有数据，将所有聊天的所有 tunnel 监听任务和各聊天的开启/关闭通知状态都给爆了
├── remove_umo (target_umo(str)): 移除特定 umo 的所有 Tunnel 监控任务及其所有关联
├── remove_tunnel (target_tunnel(str)): 移除特定 Tunnel 的主动监测任务及其所有 umo 的关联
```

## 插件输出示例
仅列出比较主要的输出，其他输出的话，为什么不自己试试呢（）

### 主动推送 Tunnel 变化
```text
✅ 监测到一个或多个 Tunnel 上线/恢复正常
🚇 test ✅
      🏷️ID: 83a3d590-4250-47e7-92f5-3040ee2e1ce9
      📶连接时间: 2026-03-25 11:30:19 (0天03时32分45秒)
      📲连接数: 2 (lax06, lax10)
      👥Replica 数: 1
      🌐当前状态: ✅ 正常 HEALTHY

🕙当前时间: 2026-03-25 15:03:04
```

### `/cft list`
```text
@test
🔍 以下是正在监测的 Tunnels 信息
🚇 test ✅
      🏷️ID: 83a3d590-4250-47e7-92f5-3040ee2e1ce9
      🐣创建时间: 2026-03-02 02:06:55
      📶连接时间: 2026-03-25 11:30:19 (0天04时06分07秒)
      📲连接数: 2 (lax06, lax10)
      👥Replica 数: 1
      ⛓️Tunnel 类型: cfd_tunnel
      🌐当前状态: ✅ 正常 HEALTHY

📦缓存更新时间: 2026-03-25 15:36:23
🕙当前时间: 2026-03-25 15:36:26
```