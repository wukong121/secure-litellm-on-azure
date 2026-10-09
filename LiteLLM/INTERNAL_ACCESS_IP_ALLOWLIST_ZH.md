# 旧公网ingress参考：公司出口IP白名单

> **不是当前West US 3 Admin门禁的修改流程。** 当前Admin通过独立Front Door Premium endpoint及PLS访问隔离native ingress，WAF固定Prevention，以否定`SocketAddr`规则阻断白名单外来源，精确批准公网IPv4 `/32`和IPv6 `/128`；内层仍须LiteLLM用户名/密码登录，不是用户Entra SSO。当前来源更新/验证按[本地Stage2–9指南](../local_execution/stage2-9-guide-zh.md)和[edge参数说明](../infra/edge/README_ZH.md)执行，不能用本文ingress注解替代WAF或将PLS NAT地址当作管理员出口。

> 目标：让 `https://<你的域名>` 这个 LiteLLM 入口**只允许公司公网出口 IP 访问**，
> 其他来源一律返回 `403 Forbidden`。域名仍解析到公网、HTTPS 证书照常使用，
> 客户在自己的 AKS 集群里几条 `kubectl` 命令即可完成，**无需 VPN / 专线**。

---

## 适用场景

| 前提 | 是否适用本方案 |
|---|---|
| 公司上网有**固定的公网出口 IP**（办公室 NAT 出口） | ✅ 适用 |
| 已有 Site-to-Site VPN / ExpressRoute 打通到 Azure | 建议改用内部 LB（本文不涉及） |
| 公司出口 IP **动态变化**（家庭宽带、拨号） | ❌ 白名单会误伤，不适用 |

本方案基于 **ingress-nginx** 的 `whitelist-source-range` 注解，属于**网络层（L3/L4）源 IP 管控**。
旧网关推理应同时使用受限Virtual Key，形成「网络 + 鉴权」双保险；不向普通用户分发Master Key。

---

## 原理

```mermaid
flowchart LR
    A[公司网内客户端<br/>出口 IP 203.0.113.10] -->|允许| C[Azure 公网 LB]
    B[公网其它来源] -->|403 拒绝| C
    C --> D[ingress-nginx<br/>whitelist-source-range 校验]
    D -->|源 IP 命中白名单| E[litellm-mi-proxy:4000]
    D -.->|源 IP 不在白名单.-> F[返回 403 Forbidden]
```

白名单是拿**真实客户端源 IP** 比对，因此必须保证 ingress-nginx 能看到真实源 IP
（见下方「关键前提」）。

---

## 前置：拿到公司公网出口 IP

在**公司网络内**的任意一台机器上执行：

```powershell
# Windows PowerShell
(Invoke-RestMethod https://api.ipify.org)
```

```bash
# Linux / macOS
curl -s https://api.ipify.org
```

- 得到的就是要放行的公网 IP。
- 公司可能有**多个出口 IP 或一整段**，请向网络管理员确认完整的出口 CIDR 段。
- 单个 IP 写成 `/32`，例如 `203.0.113.10/32`；网段例如 `203.0.113.0/24`。

---

## ⚠️ 关键前提：保留真实客户端 IP

ingress-nginx 默认跑在 Azure 公网 LB 后面。如果 controller 的 `externalTrafficPolicy`
是 `Cluster`，源 IP 可能被 SNAT 成节点内网 IP，导致白名单**误拦所有人**。
先确认并改成 `Local`：

```powershell
# 查看当前策略
kubectl get svc ingress-nginx-controller -n ingress-nginx -o jsonpath="{.spec.externalTrafficPolicy}"

# 如果不是 Local，改成 Local（保留真实客户端源 IP）
kubectl patch svc ingress-nginx-controller -n ingress-nginx `
  -p '{\"spec\":{\"externalTrafficPolicy\":\"Local\"}}'
```

---

## 启用 IP 白名单

把公司出口 IP 填入 `whitelist-source-range` 注解（逗号分隔多个 CIDR）：

```powershell
kubectl annotate ingress litellm-ingress -n litellm `
  nginx.ingress.kubernetes.io/whitelist-source-range="203.0.113.10/32" --overwrite
```

多个来源示例：

```powershell
kubectl annotate ingress litellm-ingress -n litellm `
  nginx.ingress.kubernetes.io/whitelist-source-range="203.0.113.10/32,198.51.100.0/24" --overwrite
```

> - Ingress 名默认 `litellm-ingress`、命名空间 `litellm`（与部署脚本一致）。
> - `--overwrite` 保证重复执行时覆盖旧值。
> - 修改后**立即生效**，无需重启 Pod。

---

## 完整执行顺序（复制即用）

```powershell
# 1. 保留真实源 IP
kubectl patch svc ingress-nginx-controller -n ingress-nginx `
  -p '{\"spec\":{\"externalTrafficPolicy\":\"Local\"}}'

# 2. 设置白名单（替换成公司真实出口 IP/段）
kubectl annotate ingress litellm-ingress -n litellm `
  nginx.ingress.kubernetes.io/whitelist-source-range="203.0.113.10/32" --overwrite
```

---

## 验证

```powershell
# 确认注解已写入
kubectl get ingress litellm-ingress -n litellm -o jsonpath="{.metadata.annotations}"
```

- **公司网内**访问 `https://<你的域名>/health` → 返回 `200`（正常）
- **公司网外**（如手机 4G）访问同一地址 → 返回 **`403 Forbidden`**（说明白名单生效）

---

## 撤销（恢复公网都可访问）

删除白名单注解（注意末尾的 `-` 是 kubectl 删除注解的语法）：

```powershell
kubectl annotate ingress litellm-ingress -n litellm `
  nginx.ingress.kubernetes.io/whitelist-source-range-
```

验证已删除：

```powershell
kubectl get ingress litellm-ingress -n litellm -o jsonpath="{.metadata.annotations}"
```

输出中不再出现 `whitelist-source-range` 即为成功，删除后立即恢复公网访问。

（可选）把 `externalTrafficPolicy` 还原为默认的 `Cluster`（非必须，保留 `Local` 也无害）：

```powershell
kubectl patch svc ingress-nginx-controller -n ingress-nginx `
  -p '{\"spec\":{\"externalTrafficPolicy\":\"Cluster\"}}'
```

---

## 常见问题

**Q: 加了白名单后公司内也访问不了（全 403）？**
多半是没保留真实源 IP。执行「关键前提」里的 `externalTrafficPolicy=Local`，
再用公司网内机器重新测。

**Q: 白名单该填哪个 IP？**
填**客户端的公网出口 IP**，不是内网 IP（`10.x`/`192.168.x` 无效）。
用 `https://api.ipify.org` 在公司网内查到的那个。

**Q: 公司出口 IP 会变怎么办？**
`whitelist-source-range` 是静态 CIDR，IP 变了要手动更新。
若出口 IP 频繁变动，应改用 VPN / ExpressRoute + 内部 LB 方案。

**Q: 这样安全吗？**
IP 白名单是网络层第一道防线，务必配合 LiteLLM 的 API Key 鉴权，
并将默认 `LITELLM_MASTER_KEY` 换成强随机值。

---

## 附：LoadBalancer 模式（未启用 Ingress/域名时）

如果没有走域名 + Ingress，而是裸 `LoadBalancer:4000` 暴露，
则在 Service 层用 `loadBalancerSourceRanges` 限制（Azure 会自动更新 NSG）：

```powershell
# 启用限制
kubectl patch svc litellm-mi-proxy -n litellm --type merge `
  -p '{\"spec\":{\"loadBalancerSourceRanges\":[\"203.0.113.10/32\"]}}'

# 撤销限制（置空）
kubectl patch svc litellm-mi-proxy -n litellm --type merge `
  -p '{\"spec\":{\"loadBalancerSourceRanges\":[]}}'
```
