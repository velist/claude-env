# 验收清单与操作边界

## 通过标准

`audit` 输出 12 项全 PASS,退出码 0。逐项含义:

| 项 | 通过条件 |
|---|---|
| 运行模式 | `mode=rule`,且持久化配置也是 rule |
| TUN 接管 | `tun.enable=true` |
| 境外出口 | 归属非 CN,且 ASN/城市符合所选节点 |
| 国内分流 | 国内出口与境外出口不同(或明确接受国内也走代理) |
| IPv6 泄露 | 无全局 IPv6 地址 |
| UDP/WebRTC | STUN 映射 == 境外出口 |
| DNS 接管 | `enhanced-mode=fake-ip`,境外域名解析为 198.18.x.x |
| 兜底规则 | `MATCH` 指向代理组,不是 DIRECT |
| PayPal 直连 | 存在 paypal.com 直连规则 |
| 微信直连 | 显式规则或 cn-domain 规则集覆盖 |
| 关键站点 | claude / anthropic / paypal / appleid / wechat 全部 <5s |
| 断网自救 | 看门狗已加载且心跳 <300s |

## 变更前后必做

1. `python3 scripts/claude_env.py audit` 存基线。
2. 只改一处。
3. `python3 scripts/claude_env.py retest` 看 diff,确认没有别的项被带坏。
4. 出问题先回滚这一处,不要叠加第二个改动。

## 高风险动作

以下操作会直接改变可达性,执行前必须说明影响:

- 关闭 TUN:终端和部分 App 立刻失去代理,可能同时失去 DNS 接管。
- 切换全局/direct 模式:全局会把国内流量也推过隧道。
- 修改系统 DNS:可能与 TUN 劫持冲突。
- 停止 Clash 内核:所有依赖代理的连接中断。
- 批量替换订阅:规则增强会被覆盖。

## 不该做的事

- 不要为了降低第三方风险分去清理中文字体、伪造语言环境或改系统区域。风险分不等于平台账号资格。
- 不要用单个 IP 查询网页的结论判断整机泄露。
- 不要在没确认直连可用的情况下关 TUN——会把"代理故障"变成"整机断网"。
- 不要把含节点凭据的配置文件上传到公开位置或第三方二维码服务。

## 断网后的手动恢复

若看门狗已回退:

```sh
launchctl print gui/$(id -u)/local.claude-env.guardian | head -20
tail -30 ~/Library/Logs/claude-env-guardian.log
cat "$HOME/Library/Application Support/ClaudeEnv/last-repair.json"
```

确认节点恢复后,在 Clash 里重新开启 TUN,再 `audit` 复验。

## 权限与凭据

- `~/.local/state/claude-env/` 与 guardian 状态目录权限为 0600,包含出口 IP 等运行信息。
- 节点 UUID、订阅地址不要写入本 skill 的任何文件。
