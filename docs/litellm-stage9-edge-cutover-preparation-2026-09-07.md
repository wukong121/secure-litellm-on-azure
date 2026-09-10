# 阶段9：边缘入口、试点与切流准备

> 2026-09-10审计范围更新：基础版已选择原生Spend Logs，自建L3按需选用。本文保留历史发布契约与检查，不能把旧L3条件直接视为基础版最终清单；代码中的前序证据和release检查尚待按审计模式适配，禁止跳过或伪造通过。原生正文/查询/容量/留存/故障及数据库回退、身份和协议仍需验收，详见[当前迁移指南](customer-migration-guide-zh.md)。

> 日期：2026-09-07  
> 状态：可离线实施的首轮代码与默认关闭预览；未部署、未修改DNS、未切流、未删除旧资源、未提交  
> 边界：不使用APIM或LiteLLM Enterprise，不将阶段8未完成项视为已通过

## 1. 本轮实现

| 项目 | 实现 | 默认状态 |
| --- | --- | --- |
| Front Door Premium | 单profile/endpoint、API自定义域、HTTPS私有origin | 不创建，流量开关关闭 |
| WAF | Microsoft DRS 2.1、Bot Manager 1.1、非POST规则、IP速率规则、日志scrubbing | Detection；不声称阻断生效 |
| 入口路径 | Chat、Responses、Embeddings的6个精确路径 | 无`/*`推理兜底、无管理/健康公网route |
| 回源 | HTTPS、证书名校验、源站Host为llm-api、PLS托管连接 | 连接需手工审批 |
| PLS | 仅引用已存在的API Standard internal LB frontend | 不创建、不接管LB/controller |
| 代理补充检查 | 预期X-Azure-FDID校验后仍执行Entra认证 | Stage9的未替换ID会拒绝启动 |
| 发布检查 | prepare/canary/production证据、源站快照、What-if拒绝未知变更 | 无自动部署或DNS命令 |
| 灰度/回退 | 指定批准客户端试点与手工回退Runbook | 未切流，无旧新环境自动加权 |

代码入口为`infra/edge`、`infra/edge-origin`、`deploy/components/stage9-edge`及`deploy/validation/stage9`。与之前阶段保持独立，不修改dev/test/prod overlay。主域仍为客户参数；个人验证域只存在忽略的本地配置中。

## 2. 双域名和私有源站

```text
llm-api.<域> -> Front Door Premium + WAF -> 托管Private Endpoint
            -> API PLS -> Standard internal LB -> API ingress -> API Entra代理 -> LiteLLM

llm-admin.<域> -> 企业私网/VPN/ExpressRoute -> 私有admin ingress -> OIDC管理代理
```

Front Door只关联API域，禁用默认azurefd.net路由；不创建admin custom domain/security association/route。API ingress从原Prefix `/`收敛为Exact路径和私有`/readyz`探针。管理ingress仍在独立class/namespace，不通过Front Door公开。阶段7的proxy-only后端NetworkPolicy不改变。

本轮没有安装两个controller或创建内部LoadBalancer。当前只冻结它们的契约，不能把Ingress YAML存在当作入口已经工作。controller需审核维护状态、镜像、RBAC、Host透传、SSE和超时配置，不自动沿用当前生产旧controller。

PLS模板不更改AKS托管LB，避免与cloud-controller-manager双重管理。部署前核验被引用frontend确属API-only、无publicIPAddress、LB为Standard，并审查后端池/rule/端口及admin不可达。

PLS visibility `*`只是跨订阅发现能力，自动批准列表为空。Front Door发出的连接请求需人工核对实际profile/endpoint/PLS归属后批准，requestMessage不是身份凭证。源站网络只应允许批准PLS NAT来源和健康探针，未批准VNet客户端直连也应被拒绝；本轮没有自动修改NSG或现有subnet。

API代理额外要求配置的Front Door ID，缺失/不同/重复Header拒绝；正确ID仍必须验证JWT和路径。Header不是Secret，知道值的内部客户端仍能伪造，因此不能替代PLS与NSG防绕过。`/healthz`、`/readyz`保持内部探针可用，Front Door没有这些公网route。

## 3. TLS、WAF和流式行为

- API edge仅支持HTTPS，回源HttpsOnly，`enforceCertificateNameCheck=true`，无cacheConfiguration（明确禁缓存）。
- Front Door托管证书与origin证书独立；origin必须提供覆盖`llm-api.<域>`的受信证书。证书名、SNI、Host及域所有权验证均未实测。
- Front Door创建customDomain不等于DNS已验证。`_dnsauth`、CNAME、TLS证书签发/续期需客户DNS维护者按批准窗口处理，本轮不生成自动DNS写操作。
- WAF Detection仅记录，不执行Block/速率阻断；方法与管理权限仍由代理强制执行。只有false-positive评估及审批后才切Prevention。
- 默认速率阈值600/IP/分钟只是初始待压测值，不是按Entra身份或Team配额。企业NAT共享IP、重试和Agent突发需单独校准。
- 不配置Allow短路规则、通配路径或全局managed-rule排除；业务误报例外必须限定字段/规则/有效期并留证据。
- WAF logScrubbing覆盖Header/Cookie/query/form/JSON字段；它不保证Access Log原始URI、恶意URL或全部匹配文本均无敏感值。必须实测诊断脱敏、权限与留存，不能让普通日志平台成为L3原文副本。
- originResponseTimeoutSeconds设为240，是Front Door回源等待配置，不等同于代理570秒总时长或600秒Pod退出预算。SSE首包、空闲间隔、总时长和controller缓冲必须端到端验证，不承诺所有10分钟请求可透传。
- WebSocket/Files/MCP/对象引用/加密多轮仍按阶段7拒绝；Front Door支持某协议不代表应用授权已支持。不能切换现有Codex生产入口。

## 4. 发布证据门禁

`scripts/stage9_release.py`校验环境JSON并生成本地参数；`scripts/preview_stage9.py`只执行Azure What-if。结构见`infra/edge/release.example.json`。

| phase | 流量 | WAF | 需要的主要证据 |
| --- | --- | --- | --- |
| prepare | 关闭 | Detection | 私有TLS、源站绕过拒绝、admin私网隔离、诊断隐私、PLS批准、回退计划 |
| canary | 批准客户端试点 | Detection | 再加Entra/后端ACL、阶段8采集架构、L3治理恢复、实际协议矩阵、数据库恢复、遥测告警、WAF评估和试点客户端 |
| production | 允许提出生产切流计划 | Prevention | 再加试点SLO、Prevention评审、DNS切流/回退证据 |

每项检查需要passed、report和近7天observedAt；变更工单及两个不同审批Owner必填。证据是受控流程提交的attestation，脚本不会鉴别报告真实性、验证工单签名或代替Entra PIM。Azure RBAC和GitHub Environment审批仍需独立执行；直接编辑Bicep绕过脚本的权限应被限制。

prepare要求的证据可来自先完成的PLS/origin隔离测试；尚无Front Door profile时其ID仍保留占位，流量不能开启。canary/production需要真实profile ID，生成器将其写入Stage9 API Deployment环境变量。

**阶段8阻塞仍有效**：LiteLLM OSS回调/OTEL采集架构尚未实测冻结，当前L3仍是外部代理采集；持久化/缺口目标、原文治理、全链路Trace、Content Safety效果、Sentinel及业务协议验收不能由合成测试替代。门禁明确要求这些报告，不因开始阶段9而自动放行。

## 5. 试点、切流与回退

1. 建设新私有origin但不接现有生产；保留旧环境、数据库备份和配置快照。
2. 完成Front Door域所有权和TLS校验，暂不修改现有生产主入口。新`llm-api`地址用于独立试点；DNS命名/验证操作另行批准。
3. 仅给批准客户端配置新base URL，API客户端App ID及逐主体binding控制试点范围。不得把客户端提交的Header当作灰度授权。
4. 不把旧公网AKS作为Front Door fallback origin，不将不同schema/身份/审计状态的两套环境随机分流。当前只有一个private origin，未实现按权重百分比灰度。
5. 对比成功率、429/5xx、upstream attempts、TTFT、缓存率、成本、审计缺口和权限负向测试；观察窗口与阈值由业务Owner批准。任何权限绕过或未批准审计缺口立即停止试点。
6. WAF先Detection，评审后独立窗口切Prevention，再计划更大范围客户端迁移或生产DNS切流。
7. 回退优先停止新增试点客户端，恢复旧base URL/原DNS记录；保留旧TTL快照，等待缓存到期并验证新连接去向。DNS回退不迁移已建立的SSE/WS连接。
8. 若需要停止新边缘流量，审阅将enableApiTraffic设为false的What-if，再人工执行；这会影响新请求，必须协调排空/通知。不能仅把deployEdge=false当作删除/禁用指令：Incremental模式下false不会删除既有资源。
9. 镜像降级不能回滚Prisma schema，数据已写入新库时不能直接切到旧只读库，必须按事先批准的恢复/对账方案操作，防止双写分叉。
10. 旧入口、IP、LB、VMSS业务UAMI和数据库只在稳定观察及业务/安全/数据Owner批准后退役；本轮没有任何删除脚本。

## 6. 验证入口

```bash
make validate-stage9
./.venv/bin/python scripts/render_stage7_domain.py --stage 9
kubectl kustomize temp/stage9-domain

./.venv/bin/python scripts/preview_stage9.py --resource-group <资源组> --layer edge
./.venv/bin/python scripts/preview_stage9.py --resource-group <资源组> --layer edge-origin
```

默认参数的云端What-if已执行：两个入口各为Ignore 54、Create 0、Modify 0、Delete 0，status Succeeded。仅验证关闭状态不产生资源变更；没有执行启用场景。原始结果保存在忽略的`temp/stage9-what-if`并限制文件权限，不把个人资源ID写入公共报告。

What-if校验遇到Modify/Delete/Unsupported/Deploy或未知状态直接失败，包括后续正常配置更新。必须人工检查实际delta后走独立变更评审，不能把无法分析的项目当成“安全无变更”。

编译/离线测试覆盖：默认关闭、Premium SKU、HTTPS/证书名、禁缓存、默认域禁用、API精确路径、WAF关联、PLS无自动批准、admin隔离、FDID补充检查、近期发布证据和What-if拒绝路径。CI/Makefile已接入；不自动执行Azure部署、证书操作、DNS变更或旧环境退役。

## 7. 未完成Gate

- controller选型/安装及镜像准入、两个私有LB、NSG/NAT来源限制和API-only frontend所有权；
- 启用场景What-if、Front Door/Private Link区域能力、配额成本及连接批准；
- 真实边缘/源站证书、Host/协议/SSE回归、健康探针故障与同组origin全部不健康行为；
- WAF实际误报/速率/Prevention、隐私诊断、告警和日志关联；
- 阶段7/8实际身份、L3、业务协议及数据恢复验收；
- 试点SLO、维护窗口、DNS回退与旧环境退出审批。

本轮是阶段9准备代码完成，不是已经切流或整套方案可以上线。

## 8. 本轮验证结果

- `make validate-stage9`对应的完整脚本通过，包含阶段3至8回归、L3合成演示、Bicep及参数编译、最终组合清单和OSS边界；
- Node测试31项通过；Stage9发布/模板聚焦测试12项通过；前序Python基线13项、Stage7清单/域名11项、Stage8清单3项通过；
- 两个客户域名和FDID发布生成物经Kustomize实际渲染验证，FDID仅注入API代理；
- 本地域名Stage9预览通过，个人域名和原始What-if输出仍在Git忽略目录；
- GitHub Actions语法和`git diff --check`通过；
- 两个默认关闭入口的Azure What-if各为Ignore 54、Create/Modify/Delete 0；
- 未执行启用场景What-if、controller/LB部署、PLS连接审批、证书/DNS操作、流量切换、镜像推送或Git提交。