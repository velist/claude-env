---
name: claude-env
description: 体检、复测并修复 macOS 上用于 Claude/PayPal 等境外服务的 Clash/mihomo 网络环境(规则模式、TUN、DNS、IP/IPv6/UDP 泄露、节点质量、断网自救)。当用户说环境不对、IP 泄露、Claude/PayPal 打不开、微信发不出图、代理断网或要换节点时使用。
---

# Claude 环境调教

一个 CLI 覆盖三件事:体检(`audit`)、复测对比(`retest`)、断网自救(`guardian`)。不绑定具体机场或节点,换人、换订阅只需跑一次 `doctor`。

```sh
cd ~/.codex/skills/claude-env
python3 scripts/claude_env.py doctor     # 首次:识别客户端与控制接口
python3 scripts/claude_env.py audit      # 只读,不动任何设置
```

退出码:`0` 全通过、`1` 有警告、`2` 有失败。默认只读,只有 `guardian install/uninstall` 会改系统。

## 工作顺序

1. 先 `audit`,拿到基线快照(自动存到 `~/.local/state/claude-env/last.json`)。
2. 一次只改一处,再 `retest` 看 diff。不要同时换节点、改 DNS、改规则——出了事分不清是谁的锅。
3. 任何网络变更前确认 `guardian status` 是 installed,否则先装看门狗,避免把自己搞断网。

## 命令

| 命令 | 用途 |
|---|---|
| `doctor [--write-config]` | 识别客户端/控制接口/运行时配置,生成 `~/.config/claude-env/config.json` |
| `audit` | 12 项体检,输出带修复建议 |
| `retest` | 复测并与上次快照逐项对比 |
| `services` | 只测 claude/anthropic/paypal/appleid/wechat |
| `nodes [--group X] [--probe]` | 代理组当前指向 + 节点延迟排名 |
| `guardian status\|run --check` | 看门狗状态,只读 |
| `guardian install [--migrate]` | 安装 launchd 每 60s 巡检;`--migrate` 接管旧看门狗 |
| `guardian uninstall` | 卸载 |

## 换环境时

别人用只需要三步:

1. `doctor` —— 自动探测 Clash Verge Rev / Mihomo Party / ClashX Meta / 任意 mihomo 控制端口。
2. 探不到或有多个客户端时,`doctor --write-config` 生成模板,填 `controller`、`group`、`runtime_config`。
3. `audit` —— 所有判断都基于运行时实测(出口 IP、fake-ip 解析、STUN 映射、站点 TLS),不依赖节点名或分组命名。

配置项:`controller`(unix 或 tcp+secret)、`runtime_config`、`group`、`services`(自定义站点字典)。

## 判定口径(最容易搞错的地方)

- **国内站点看到真实 IP 不是泄露。** CN 域名/GeoIP 直连是刻意设计;泄露只看境外回显、STUN 映射、IPv6 这三处是否等于代理出口。
- **系统 DNS 显示 114.114.114.114 不是泄露。** 53 端口被 TUN 劫持,查询实际走 mihomo。判据是 fake-ip 是否生效 + 境外解析出口。
- **ping 低不等于能用。** 要看 TLS 握手和完整下载:有的节点握手就超时,延迟却很好看。
- **ASN 是家宽还是机房,决定风控风险,不决定合法性。** HiNet/中华电信这类 ISP 段比小机房更像真人线路,但不保证不被标记。
- **403/Cloudflare 验证页说明网络通**,不是断网,不要因此去"修"网络。

## 改配置的纪律

- 改规则优先用「配置覆写」里的前置规则,不要直接编辑订阅文件(会被覆盖)。
- 加规则只加必要域名后缀,不要整段 `DOMAIN-KEYWORD` 误伤。
- 关 TUN、切全局、改 DNS 都属于高风险动作:先说明影响,改完立刻 `retest`。
- 涉及具体故障的处置细节看 [references/casebook.md](references/casebook.md)。
- 验收标准和操作边界看 [references/checklist.md](references/checklist.md)。

## 看门狗行为

每 60s 探测 4 个站点;单侧异常只记录;连续 3 轮全失败 + 距上次修复 ≥10 分钟 + 网络与配置指纹未变 + 物理直连可用,才动 TUN/DNS。修复前落快照到 `~/Library/Application Support/ClaudeEnv/last-repair.json`。

它恢复的是**本地上网**,可能暂时失去代理;不会自动换国家、不会切全局、不会杀进程。
