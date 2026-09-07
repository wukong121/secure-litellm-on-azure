# LiteLLM 网关当前版与安全增强版 BOM 成本对比

> 文档状态：方案审查估算，不是 Azure 报价单
> 估算日期：2026-08-31
> 价格来源：Azure Pricing MCP / Azure Retail Prices API
> 币种：USD，公开零售价，Pay-As-You-Go
> 估算区域：`westus`；Front Door、Private Link、DNS 等全球服务按对应全球/Zone 1 meter
> 月度换算：`730 小时/月`、`30 天/月`
> 目标口径：小型生产基线 + Azure Front Door Premium + 用量项只列单价
> 明确边界：不包含 APIM

## 1. 执行摘要

按本文假设，当前 LiteLLM 网关基础设施固定成本约为：

```text
当前版：约 $319/月
```

安全增强版有两个主要成本场景：

```text
安全增强版（复用客户现有 Hub Firewall）：约 $1,206/月
安全增强版（为 LiteLLM 独立部署 Firewall Premium）：约 $2,484/月
```

对应固定成本增量约为：

| 场景 | 固定月费 | 相对当前版增量 | 约数倍数 |
| --- | ---: | ---: | ---: |
| 当前版 | $319 | - | 1.0x |
| 安全增强版，复用 Hub Firewall | $1,206 | +$887 | 3.8x |
| 安全增强版，专属 Firewall Premium | $2,484 | +$2,165 | 7.8x |

安全版增量主要不是 LiteLLM 软件许可或 AKS 节点，而是：

1. Azure Firewall Premium：约 `$1,277.50/月`；
2. Azure Front Door Premium：基础费 `$330/月`；
3. PostgreSQL Flexible Server Zone-Redundant HA：计算约 `$296.38/月`，另加存储；
4. Private Endpoint、ACR Premium、Defender 和额外 AKS 节点；
5. 日志、Sentinel、AI Content Safety、PII 和员工/Agent 原文审计等按量费用。

如果客户已有共享 Hub Firewall、Log Analytics、Sentinel、Private DNS 和网络平台，应按资源标签或流量分摊计算增量成本，不应把共享平台全部费用计入 LiteLLM。

## 2. 估算假设

### 2.1 当前版 BOM 假设

根据仓库、handoff 和已验证环境，当前版采用：

- AKS Standard tier；
- 2 台 `Standard_D2s_v3` Linux 节点；
- 每节点 1 块 P10 Premium SSD OS Disk；
- LiteLLM 单副本；
- AKS 内单副本 PostgreSQL；
- PostgreSQL PVC按 P4 Premium SSD 计费；
- ingress-nginx 使用 Standard Load Balancer；
- 1 个 Standard Static Public IPv4；
- ACR Basic；
- cert-manager 与 Let's Encrypt 无单独 Azure 资源月费；
- 节点级 UAMI、Kubernetes ConfigMap/Secret 无单独资源费。

注意：部署脚本默认节点数仍可配置为 1。本文按 handoff 中已运行的 2 节点状态估算；若实际客户环境只有 1 台节点，当前版固定成本约减少 `$85.41/月`。

### 2.2 安全增强版 BOM 假设

本次采用“小型生产基线”：

- AKS Standard tier；
- 3 台 `Standard_D2s_v5` Linux 节点，跨可用区/节点分布；
- 每节点 1 块 P10 Premium SSD OS Disk；
- LiteLLM 至少 2 副本；
- Azure Front Door Premium + WAF；
- AKS ingress 使用 Internal Standard Load Balancer；
- Azure Firewall Premium，或复用企业现有 Hub Firewall；
- PostgreSQL Flexible Server General Purpose `Standard_D2ds_v5`，2 vCore，Zone-Redundant HA；
- PostgreSQL 主存储 128 GB；
- Azure Managed Redis `Balanced_B0`；
- ACR Premium；
- Key Vault Premium，低操作量；
- 7 个 Private Endpoint：3 个 Azure OpenAI/Foundry、Key Vault、ACR、PostgreSQL、Redis；
- 6 个 Private DNS Zone；
- Defender for Containers，按 3 节点合计 6 vCore；
- Log Analytics、Sentinel、Managed Prometheus、Front Door流量、Firewall流量和 AI Guardrail按量计费；
- 员工/Agent L3 原文审计默认关闭，不计固定容量。

上述是审查基线，不代表最终生产 sizing。节点、数据库、Redis、Private Endpoint 和日志量应在压测及客户 RTO/RPO 确认后调整。

## 3. 当前版 BOM 与固定成本

| 组件 | SKU/数量 | Azure Retail 单价 | 月估算 | 说明 |
| --- | --- | ---: | ---: | --- |
| AKS 控制面 | Standard × 1 | $0.10/小时 | $73.00 | Standard Uptime SLA |
| AKS 节点 | `Standard_D2s_v3` Linux × 2 | $0.117/小时 | $170.82 | 不含 Savings Plan/Reservation |
| AKS OS Disk | P10 LRS × 2 | $19.71/月 | $39.42 | 默认磁盘实际规格需现场核对 |
| PostgreSQL PVC | P4 LRS × 1 | $5.28/月 | $5.28 | P4按 32 GiB meter，当前逻辑容量可能更小 |
| ACR | Basic × 1 | $0.1666/天 | $5.00 | 不计超额存储/出口 |
| Standard Load Balancer | 1 个，含规则 | 约 $0.03/小时 | $21.90 | MCP返回值按显示精度估算 |
| Public IPv4 | Standard Static × 1 | $0.005/小时 | $3.65 | ingress 公网入口 |
| UAMI / ConfigMap / Secret | 若干 | 无固定资源费 | $0.00 | 不代表没有安全风险 |
| **固定月费合计** |  |  | **$319.07** | 约 `$319/月` |

### 当前版未计入项

- Azure OpenAI/Foundry 模型 Token；
- 公网数据传输；
- Azure DNS 公网 Zone/域名；
- ACR 超额存储和镜像拉取流量；
- 企业协议价、税费、支持计划和人员运维成本。

## 4. 安全增强版 BOM 与固定成本

| 组件 | SKU/数量 | Azure Retail 单价 | 月估算 | 相对当前版 |
| --- | --- | ---: | ---: | ---: |
| AKS 控制面 | Standard × 1 | $0.10/小时 | $73.00 | $0.00 |
| AKS 节点 | `Standard_D2s_v5` Linux × 3 | $0.112/小时 | $245.28 | +$74.46 |
| AKS OS Disk | P10 LRS × 3 | $19.71/月 | $59.13 | +$19.71 |
| Internal Load Balancer | Standard × 1 | 约 $0.03/小时 | $21.90 | $0.00 |
| ACR | Premium × 1 | $1.6666/天 | $50.00 | +$45.00 |
| Front Door + WAF | Premium × 1 | $330/月 | $330.00 | +$330.00 |
| Azure Firewall | Premium × 1 | $1.75/小时 | $1,277.50 | +$1,277.50 |
| PostgreSQL HA计算 | `Standard_D2ds_v5` 2 vCore × 2 | $0.203/小时/实例 | $296.38 | +$296.38 |
| PostgreSQL 主存储 | 128 GB | $0.14/GB/月 | $17.92 | +$17.92 |
| Azure Managed Redis | `Balanced_B0` × 1 | $0.02/小时 | $14.60 | +$14.60 |
| Private Endpoint | 7 个 | $0.01/小时/个 | $51.10 | +$51.10 |
| Private DNS Zone | 6 个 | $0.50/Zone/月 | $3.00 | +$3.00 |
| Defender for Containers | 6 vCore | MCP显示约 $0.01/vCore/小时 | $43.80 | +$43.80 |
| Key Vault Premium | 1 个 | 无基础费；按操作/Key | $0.00 | 按量 |
| **专属 Firewall 固定合计** |  |  | **$2,483.61** | **+$2,164.54** |
| **复用 Hub Firewall 固定合计** | 去除专属 Firewall |  | **$1,206.11** | **+$887.04** |

### 固定合计的解释

- “复用 Hub Firewall”不是 Firewall 免费，而是其基础部署费已由企业共享网络平台承担；LiteLLM 仍应分摊数据处理、日志和运营成本；
- PostgreSQL HA按主/备两份 2 vCore计算。最终账单还取决于备份、IOPS、存储增长和实际 HA模式；
- Private Endpoint按 7 个估算。每新增一个模型资源或私有依赖，固定成本约增加 `$7.30/月`，另加数据处理；
- Key Vault没有基础月费，低操作量对固定合计影响很小；若使用 HSM key、自动轮换或高操作量需另计；
- Defender MCP单价显示精度有限，正式报价应在 Azure Calculator按订阅和计划复核；
- Front Door Premium基础费已经包含 WAF能力，但请求、流量、Bot/CAPTCHA和规则执行仍可能按量计费。

## 5. 按量计费项

用户选择“只列单价”，因此以下项目不进入固定月费合计。

| 服务 | Azure Retail 单价/口径 | 成本驱动因素 | 备注 |
| --- | ---: | --- | --- |
| Front Door Premium 请求 | 约 $0.01/10K 请求 | API请求量、WAF处理 | Zone 1 meter；不同阶梯可能变化 |
| Front Door Premium入口数据 | $0.02/GB | 客户端上传 Prompt/文件 | 以 Retail meter为准 |
| Front Door Premium出口数据 | 约 $0.01-$0.08/GB | 响应、区域和用量阶梯 | Zone 1 多阶梯 meter |
| Azure Firewall Premium数据处理 | $0.016/GB | 受控 egress/ingress | 不含 `$1.75/小时` 部署费 |
| Private Link数据处理 | $0.01/GB ingress + $0.01/GB egress | PE流量 | Endpoint小时费已计固定成本 |
| Key Vault普通操作 | $0.03/10K 操作 | Secret读取/写入 | Premium与Standard普通操作同 meter |
| Key Vault高级 Key操作 | $0.15/10K 操作 | HSM/高级加密操作 | HSM key本身另计 |
| PostgreSQL超额备份 | $0.095/GB/月 | 超出免费备份额度的数据 | PITR窗口和变更率影响明显 |
| Log Analytics ingestion | $2.99/GB | AKS、WAF、Firewall、PG、Redis、LiteLLM 日志量 | 最大可控用量项之一 |
| Log Analytics额外保留 | $0.13/GB/月 | 超过包含期的热日志 | Archive/Search另计 |
| Microsoft Sentinel | $5.59/GB analysis | 安全分析数据量和计划 | 需确认与 Log Analytics 的组合计费，避免重复估算 |
| Managed Prometheus | 按 samples ingestion/query | 指标基数、抓取频率、保留 | Pricing MCP返回精度不足，正式报价用 Calculator |
| Azure AI Content Safety | 按文本/图片交易 | 请求数、文本块数、Prompt Shields | Pricing MCP服务族解析失败，正式报价用 Calculator |
| Azure AI Language PII | 按文本记录/字符计费层级 | 审计/Guardrail文本量 | 需按目标 SKU和区域用 Calculator确认 |
| ADLS/Blob L3审计 | 按容量、写入、读取、检索和出口 | Prompt/Response/tool原文体积与留存 | 默认关闭；不进入 PG |
| Azure OpenAI/Foundry | 按模型 Token/图片/音频/工具 | 实际模型使用 | 两版共同业务成本，未纳入基础设施对比 |

## 6. 员工与 Agent 上下文审计成本影响

`SEC-21` 的 L3 原文审计会显著改变成本结构，不应简单打开 `store_prompts_in_spend_logs=true` 后继续使用同一 PostgreSQL。

| 审计层级 | 建议存储 | 固定/按量 | 成本特点 |
| --- | --- | --- | --- |
| L1 元数据 | Spend Logs + Log Analytics | 主要按量 | 每次调用一条元数据，适合全量保留 |
| L2 脱敏摘要/标签 | Log Analytics 或 ADLS | 按量 | 体积小于原文，可用于趋势和风险分析 |
| L3 Prompt/Response/tool原文 | 独立 ADLS/Blob | 按量 | 受上下文长度、Agent轮数、文件/图片和留存期影响最大 |

### 审计成本必须采集的输入

在给出 L3 月费前，需要至少 7-14 天试点数据：

- 月请求量；
- 平均/峰值 Prompt 与 Response 字节数；
- Agent平均轮数、tool call/result数量；
- WebSocket/SSE是否保存每帧、每 chunk或最终聚合结果；
- 图片、音频、文件和 base64占比；
- L1/L2/L3各自留存期；
- 查询、导出、Legal Hold和再处理频率；
- PII/Secret/Content Safety扫描的文本记录数。

为避免再次发生 PG 满盘：

- `store_prompts_in_spend_logs` 保持 `false`；
- L3通过异步 callback输出到独立存储；
- PG仅保留可关联的 Call ID、身份、模型、Token、费用和对象引用；
- 对 PG、Log Analytics和L3存储分别设置 70%/85% 容量或预算告警。

## 7. 成本敏感性

### 7.1 主要固定成本开关

| 决策 | 月成本影响 | 安全/架构影响 |
| --- | ---: | --- |
| 复用企业 Hub Firewall | 约 -$1,277.50 | 推荐在客户已有合规 Hub 时复用，不建议直接取消 egress治理 |
| AKS 3 节点改为 2 节点 | 约 -$101.71 | 降低跨区/维护冗余；需重新验证 PDB和容量 |
| ACR Premium降级 Basic | 约 -$45.00 | Basic不满足 Private Link目标，不建议生产降级 |
| Front Door Premium取消 | 约 -$330.00 | 失去目标公网 WAF/私有源站设计；纯内网场景应改用 App Gateway WAF v2重新计价 |
| PostgreSQL取消 HA | 约 -$148.19 | 恢复数据库单点，不符合当前 P0目标 |
| Private Endpoint每增减 1 个 | 约 +/-$7.30 | 与私网依赖数量直接相关 |

### 7.2 可优化但不应牺牲基线的项目

- AKS节点和 PostgreSQL稳定后评估 1 年/3 年 Reservation 或 Savings Plan；
- 使用 HPA/Cluster Autoscaler在满足最小可用副本前提下降低空闲节点；
- 日志按 L1/L2/L3分层，减少高价 Analytics Logs摄入；
- 对 WAF、Firewall、Private Link和审计流量建立月度基线与预算告警；
- 复用客户已有 Hub、DNS、Log Analytics、Sentinel和CI/CD平台；
- 对开发/测试环境使用更小规格、非 HA数据库和工作时间启停，但生产验收环境不能因此失真。

## 8. 不在本估算中的费用

- 客户自有Entra认证代理的开发运维成本，以及经批准的Azure AI Guardrail/观测服务成本；本方案不包含LiteLLM Enterprise许可证；
- Microsoft Entra ID P1/P2、Conditional Access、PIM、Purview等用户/租户许可；
- Azure支持计划、税费、EA/MCA折扣、Azure Hybrid Benefit；
- 域名、公共证书或客户自有 PKI；
- CI/CD runner、代码扫描、签名服务和人员实施运维成本；
- 跨区域、跨可用区、Internet和 ExpressRoute/VPN数据传输；
- Azure OpenAI/Foundry模型消费与配额；
- 多区域 DR、Geo Backup和第二套 Warm Standby环境；
- L3全文审计和 Legal Hold的实际容量与分析成本。

## 9. 价格依据

本文已通过 Azure Pricing MCP 查询以下 Retail meter：

| 服务/SKU | Retail meter | 单价 |
| --- | --- | ---: |
| VM `Standard_D2s_v3` Linux | D2s v3 | $0.117/小时 |
| VM `Standard_D2s_v5` Linux | D2s v5 | $0.112/小时 |
| AKS Standard | Standard Uptime SLA | $0.10/小时 |
| Premium SSD P4 LRS | P4 LRS Disk | $5.28/月 |
| Premium SSD P10 LRS | P10 LRS Disk | $19.71/月 |
| ACR Basic | Basic Registry Unit | $0.1666/天 |
| ACR Premium | Premium Registry Unit | $1.6666/天 |
| Front Door Premium | Premium Base Fees | $330/月 |
| Azure Firewall Premium | Premium Deployment | $1.75/小时 |
| PostgreSQL Ddsv5 2 vCore | vCore | $0.203/小时/实例 |
| PostgreSQL Flex Storage | Storage Data Stored | $0.14/GB/月 |
| Azure Managed Redis B0 | B0 Cache Instance | $0.02/小时 |
| Private Endpoint | Standard Private Endpoint | $0.01/小时 |
| Standard Public IPv4 | Standard IPv4 Static Public IP | $0.005/小时 |
| Private DNS | Private Zone | $0.50/Zone/月 |
| Log Analytics | Analytics Logs Data Ingestion | $2.99/GB |
| Microsoft Sentinel | Pay-as-you-go Analysis | $5.59/GB |
| Key Vault | Operations | $0.03/10K |

零售价会变化，且不同区域、计费层级和协议价可能不同。正式预算前应：

1. 用客户目标订阅和区域重新运行 Azure Pricing MCP；
2. 用 Azure Pricing Calculator复核 Pricing MCP未返回或精度不足的项目；
3. 从 Azure Cost Management导出现网 30-90 天实际账单；
4. 用压测/试点得到日志、流量、Guardrail和L3审计用量；
5. 按 EA/MCA价格、Reservation/Savings Plan、税费和共享平台分摊形成最终 TCO。

## 10. 审查时需要确认

| 决策 | 当前估算 | 待确认内容 |
| --- | --- | --- |
| 当前 AKS节点数 | 2 | 客户现场是否一致 |
| 安全版 AKS节点 | 3×D2s_v5 | 压测后是否需 D4s_v5或更多节点 |
| Firewall | 同时给专属/共享两种 | 客户是否已有合规 Hub Firewall |
| PostgreSQL | D2ds_v5 2 vCore HA + 128 GB | RTO/RPO、连接数、IOPS、增长率 |
| Redis | Balanced_B0 | 内存、连接数、HA/SLA要求 |
| Private Endpoint | 7 个 | 实际模型资源数和共享方式 |
| Private DNS Zone | 6 个 | 是否复用企业中心 DNS Zone |
| 日志/Sentinel | 只列单价 | 预计 GB/月和留存期 |
| AI Guardrail | 只列成本驱动 | 月交易量、文本块和目标 SKU |
| 员工/Agent L3审计 | 默认关闭 | 采集范围、留存、查询、合规和用量 |