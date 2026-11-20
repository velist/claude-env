# 故障案例簿

每条都是实测结论,不是推测。复现方法一并写下,方便下次直接验证。

## PayPal 打不开(转圈 / 无法访问此网站)

**症状**:浏览器和 iPad 都打不开 paypal.com,其他境外站点正常。

**判据**:
```sh
curl -sS -m 10 -o /dev/null -w 'code=%{http_code} tls=%{time_appconnect} total=%{time_total}\n' https://www.paypal.com/
```
经节点 `tls` 阶段超时(>10s);直连 0.3–0.4s 正常。说明是该节点到 PayPal 的握手被阻断,不是账号或 DNS 问题。

**处置**:加两条前置直连规则
```
DOMAIN-SUFFIX,paypal.com,DIRECT
DOMAIN-SUFFIX,paypalobjects.com,DIRECT
```

**副作用**:PayPal 会按出口 IP 跳地区页(国内直连会跳 `/cn/home`)。需要特定地区页时再单独讨论,不要为了地区把整站改回代理。

## iCloud 添加账户卡在 "Signing in..."

**判据**:直连下载 `https://appleid.apple.com/sign-in` 只有 ~20KB/s,5–13 秒且经常在 150–180KB 处 stall;走台湾节点稳定 1.7–2.2 秒,219KB 完整返回。认证页永远加载不完,所以一直转圈。

```sh
for i in 1 2 3; do curl -sS -m 20 -o /dev/null -w "%{http_code} %{time_total} %{size_download}\n" https://appleid.apple.com/sign-in; done
```

**处置**:把承载 Apple 域名的代理组从「直接连接」改为走代理组。Apple 域名全部经隧道后登录秒过。

**两个坑**:
- 卡死的 `AKAuthorizationRemoteViewService` 进程能存活数小时并让后续每次点击都复用它。用 `ps aux | grep AKAuthorizationRemoteViewService` 找 PID,`kill -9`。
- macOS 每个用户只允许一个 iCloud 主账号。第二个 Apple ID 只能加在「系统设置 → 你的名字 → 媒体与购买项目」,加到「互联网账户 → iCloud」会失败。

## 微信图片发不出去

**原因**:图片 CDN 走了代理,大文件被节点限速/重置。

**处置**:`qpic.cn`、`qlogo.cn`、`weixin.qq.com`、`wechat.com` 直连。若订阅自带 cn-domain 规则集,通常已覆盖,不必重复添加。

## "我的 IP 泄露了"

先分清三种情况,再动手:

1. **国内站点显示电信/联通 IP** —— 正常。`cn-domain`、`GeoIP,CN` 直连就是设计目标。
2. **境外回显显示国内 IP** —— 真泄露。看 `MATCH` 规则是否指向 DIRECT,或该域名被 `cn-domain` 误匹配。
3. **IPv6 / STUN 显示真实 IP** —— 真泄露。查全局 IPv6 地址、UDP 是否走代理。

本工具的 `audit` 会同时给出境外出口、CN 出口、IPv6 和 STUN 映射四项,直接对照即可,不要凭单个网页下结论。

### WebRTC 检测页显示国内真实 IP

**症状**:泄露检测页一边写"未发现真实 IP 泄露",一边在 WebRTC 区域列出你的电信/联通 IP。

**原因**:WebRTC 会同时向多个 STUN 服务器发 UDP 探测包,谁先回就用谁。国内 STUN(如 `stun.chat.bilibili.com`、`stun.hitv.com`、`stun.miwifi.com`)命中 `cn-domain` / `GeoIP,CN` 后走 DIRECT,对端看到的自然是你家的真实出口。所以"有时显示有时不显示",取决于检测站用了哪家 STUN。

**验证**:
```sh
# 国外 STUN 正常 → 节点 IP
# 国内 STUN 泄露 → 真实 IP
```
`audit` 的「国内 STUN 泄露」一项就是干这个的。

**处置**:在前置规则最上面加
```
DOMAIN-KEYWORD,stun,<代理组名>
DOMAIN-SUFFIX,stunprotocol.org,<代理组名>
```
代价:STUN 走代理后,浏览器视频通话会多一跳延迟;微信/腾讯会议若出现卡顿,把对应域名单独放回直连。

注意 `DOMAIN-KEYWORD,stun` 会顺带匹配 `student.*` 这类含 "stun" 的域名,介意的话换成 `DOMAIN-REGEX,^stun[0-9]*\.`。

## 系统 DNS 显示 114.114.114.114

不是泄露。TUN 的 `dns-hijack: any:53` 会把所有 53 端口查询接管到 mihomo,`dig` 也拿不到真实上游。判据只有两个:`enhanced-mode` 是否为 fake-ip、境外域名是否返回 `198.18.x.x`。

## 节点延迟好看但打不开

延迟由 provider 每 10 分钟健康检查得出,测的是 `generate_204`,不代表目标站点可用。遇到特定站点失败,用 `services` 单独测该站点的 TLS 时间,再决定是否给该域名单独分流。

## 家宽 IP 与机房 IP

判断方法:查节点出口 IP 的 ASN。家宽/ISP 段(如各地电信、有线电视宽带)风控上更像真人;小型机房 ASN 延迟更低,但更容易被判定为代理。

两者都能用,差别在容易被风控的站点(支付、订阅、账号注册)。这类场景优先选 ISP 段。

## 全局模式为什么不能用

全局会把微信、网银、国内 CDN 一起推过隧道:延迟上升、图片发不出、部分国内服务直接拒绝。规则模式 + 兜底指向代理组,效果等价于"境外全代理",副作用小得多。

## iPad 扫码导入完整配置

App Store 版 Clash for iOS 的扫码入口支持的不只是分享链接:

1. 配置 → 配置库 → ⊕ → 扫描二维码
2. 扫到 YAML 文本时会提示「这是 YAML 文本,不是配置链接」,点「移到文件」
3. 命名保存,启用即可,规则原样保留

纯节点分享码(vmess://)扫进去只有节点,不带规则,分流要重新配。

## 验证路径时别用错工具

- `curl --noproxy '*'`:走系统/TUN 路径,等同普通 App。
- `curl -x http://127.0.0.1:7897`:仍然按规则分流,不能拿来"指定某个节点";要换节点只能改组选择。
