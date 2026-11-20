# claude-env

给 Claude / PayPal 这类境外服务用的 macOS 网络环境体检、复测与断网自救工具。

一个命令行,十三项检查,一条看门狗。默认只读,不会乱改你的网络。

```
✅ 运行模式         rule 规则模式
✅ TUN 接管        已开启
✅ 境外出口         198.51.100.23 TW Taipei AS0000 Example ISP
✅ 国内分流         国内直连出口 203.0.113.7 中国 广西 南宁 电信
✅ IPv6 泄露       无全局 IPv6 地址
✅ UDP/WebRTC 出口  STUN 映射 198.51.100.23
✅ 国内 STUN 泄露  国内 STUN 也走代理(198.51.100.23)
✅ DNS 接管         fake-ip 实测生效(解析为 198.18.x),系统 DNS 114.114.114.114
✅ 兜底规则         MATCH -> 国外流量
✅ PayPal 直连      paypalobjects.com,paypal.com
✅ 微信直连         由 cn-domain 规则集覆盖(国内域名直连)
✅ 关键站点         claude / anthropic / paypal / appleid / wechat 均正常
✅ 断网自救         运行中,上次 26.6s 前,状态 healthy

结果:12 通过 / 0 警告 / 0 失败
```

## 致谢与来源

本项目的**问题起点、检测维度与验证思路**来自 [LinXiaoTao/FuckClaude](https://github.com/LinXiaoTao/FuckClaude)(MIT License)。

FuckClaude 做的是在浏览器里扫描“你像不像一个中国用户”:时区、语言、字体、代理特征等,并给出风险分。它把问题定义得很清楚——**Claude 判定环境,看的是这些可观测信号**。

本项目不是它的复刻,也不包含它的代码。它把同一个问题从浏览器往下推了一层:

| | FuckClaude | claude-env |
|---|---|---|
| 检测层 | 浏览器指纹、时区、语言、字体 | 系统网络层:出口 IP、DNS、IPv6、UDP/STUN |
| 输出 | 风险分 + 命中信号 | 逐项 PASS/WARN/FAIL + 修复建议 |
| 动作 | 只读诊断 | 只读诊断 + 复测对比 + 断网自救 |
| 面向 | 任何人打开网页 | 用 Clash/mihomo 的 macOS 用户 |

浏览器扫描告诉你“像不像”,网络体检告诉你“通不通、漏不漏、断没断”。两个工具解决的是同一条链路上的不同环节,建议配合使用。

原项目版权归 LinXiaoTao 所有,遵循 MIT License。本项目的独立实现同样以 MIT 发布,详见 [LICENSE](LICENSE)。

## 它检查什么

| 检查 | 判据 |
|---|---|
| 运行模式 | `mode=rule`;全局会把国内流量也推过隧道 |
| TUN 接管 | 否则终端和部分 App 会绕过代理 |
| 境外出口 | 归属非 CN,并显示 ASN/机房 |
| 国内分流 | 国内出口与境外出口不同 |
| IPv6 泄露 | 无全局 IPv6 地址 |
| UDP/WebRTC | STUN 映射地址 == 境外出口 |
| 国内 STUN | 国内 STUN 服务器看到的出口 == 境外出口 |
| DNS 接管 | fake-ip 实际生效 |
| 兜底规则 | `MATCH` 指向代理组而非 DIRECT |
| PayPal 直连 | 部分节点到 PayPal 的 TLS 握手会被阻断 |
| 微信直连 | 图片 CDN 走代理容易发不出去 |
| 关键站点 | claude / anthropic / paypal / appleid / wechat 的 TLS 与完整下载 |
| 断网自救 | 看门狗已加载且心跳正常 |

## 安装

需要 macOS、Python 3.9+、Clash Verge Rev / Mihomo Party / ClashX Meta 或任意带 `external-controller` 的 mihomo 内核。

```sh
git clone https://github.com/velist/claude-env.git ~/.codex/skills/claude-env
cd ~/.codex/skills/claude-env
python3 scripts/claude_env.py doctor
```

`doctor` 会自动探测客户端和运行时配置。探不到时有提示,也可以手写 `~/.config/claude-env/config.json`:

```json
{
  "controller": { "kind": "tcp", "host": "127.0.0.1", "port": 9090, "secret": "" },
  "runtime_config": "/path/to/runtime.yaml",
  "group": "国外流量"
}
```

## 使用

```sh
python3 scripts/claude_env.py audit                      # 12 项体检,只读
python3 scripts/claude_env.py retest                     # 复测并与上次逐项对比
python3 scripts/claude_env.py services                   # 只测关键站点
python3 scripts/claude_env.py nodes --group "国外流量"     # 组指向 + 节点延迟
python3 scripts/claude_env.py guardian status            # 看门狗状态
python3 scripts/claude_env.py guardian install           # 安装每 60s 巡检
```

退出码 `0` 全通、`1` 有警告、`2` 有失败,可以直接接进脚本。

只有 `guardian install/uninstall` 会改系统,其余全部只读。

## 三个容易搞错的判断

1. **国内站点看到真实 IP 不是泄露。** CN 域名直连是设计目标,泄露只看境外回显、STUN、IPv6。
2. **系统 DNS 显示 114.114.114.114 不是泄露。** 53 端口被 TUN 劫持,判据是 fake-ip 是否生效。
3. **延迟低不等于能用。** 有的节点握手就超时,ping 却很好看。要看 TLS 时间和完整下载字节数。
4. **WebRTC 泄露常常来自国内 STUN 服务器。** 它们命中直连规则,会把真实出口报给检测页,而国外 STUN 一切正常——这就是"有时显示有时不显示"的原因。

更多实战案例见 [references/casebook.md](references/casebook.md),验收标准见 [references/checklist.md](references/checklist.md)。

## 断网自救

看门狗每 60 秒探测 4 个站点。单侧异常只记录;必须同时满足:

- 连续 3 轮全部失败
- 距上次修复 ≥ 10 分钟
- 网络接口与代理配置指纹未变
- 物理直连本身可用(排除运营商故障)

才关闭故障 TUN 并把系统 DNS 改为 223.5.5.5 / 119.29.29.29。修复前落快照,恢复的是本地上网,可能暂时失去代理,不会自动换国家,也不会杀进程。

```sh
tail -30 ~/Library/Logs/claude-env-guardian.log
cat "$HOME/Library/Application Support/ClaudeEnv/last-repair.json"
```

## 不做的事

- 不为了降低风险分去清理字体、伪造语言环境或改系统区域。风险分不等于平台账号资格。
- 不根据单个 IP 查询网页下结论。
- 不在没确认直连可用的情况下关 TUN。
- 不上传节点凭据,二维码只在本机生成。

## 兼容性

已验证:macOS 26 / Apple Silicon、Clash Verge Rev 2.x、mihomo 1.19。

理论支持:所有暴露 mihomo 控制接口的客户端;TCP 控制器需填 `secret`。

## License

MIT。原项目 [FuckClaude](https://github.com/LinXiaoTao/FuckClaude) 亦为 MIT,版权归 LinXiaoTao。

---

## English

`claude-env` audits, re-tests and rescues the macOS network stack behind Claude / PayPal access: rule vs global mode, TUN, egress IP and ASN, DNS hijacking, IPv6 and STUN leaks, per-service TLS health, and a watchdog that restores local connectivity without thrashing your config.

Inspired by [LinXiaoTao/FuckClaude](https://github.com/LinXiaoTao/FuckClaude), which scans the browser-side signals Claude may look at. This project is an independent implementation that goes one layer down, into the system network path. No code is shared; the original is MIT-licensed and credited above.

```sh
git clone https://github.com/velist/claude-env.git ~/.codex/skills/claude-env
python3 ~/.codex/skills/claude-env/scripts/claude_env.py doctor
```
