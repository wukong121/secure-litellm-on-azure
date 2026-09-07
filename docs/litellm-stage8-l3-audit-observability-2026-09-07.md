# LiteLLM阶段8：L3原文审计、可观测性与安全检测

> 日期：2026-09-07  
> 状态：首期代码与离线验证完成；未部署、未切流、未提交，未采集生产原文  
> 主线：L1关联 -> L3采集/索引/授权查看/留存删除 -> 输入Content Safety -> 检测模板  
> 仅使用Azure服务、客户自有代码与OSS依赖，不使用LiteLLM Enterprise或APIM

## 后续收尾说明（2026-09-07）

本文记录首期Node代理采集实现，不能理解为L3全部编码或生产集成完成。后续[OSS回调实测](litellm-oss-callback-validation-2026-09-07.md)已验证固定版本的免费CustomLogger及未接入部署的薄适配器；正常内容可提取，但上游EOF仍可触发success，接收器异常也不阻止模型200。下一步采用模型回调、可信入口事实与独立可靠交付层的分工，当前采集链路保持不变。

[代码收尾台账](litellm-code-completion-backlog-2026-09-07.md)列出受信接收器、持久化恢复、协议、脱敏预览、保全竞态与监控接线等上线阻断项。以下默认关闭、RAM缓冲和未完成云端验收的限制仍然成立。

## 1. 交付范围

| 范围 | 本轮交付 | 尚不能宣称 |
| --- | --- | --- |
| L1 | 服务端Trace/Request ID、完成/断连元数据、手动OpenTelemetry Span及Collector模板 | 全链路每一层都已产生关联Span |
| L3采集 | 已授权请求、规范化转发请求、JSON/SSE响应、部分/截断状态 | 完整Codex/Agent会话或隐藏推理审计 |
| 存储 | Azure Blob SDK适配器、独立CMK私有账户及分离RBAC模板 | Azure写入和CMK私网权限已实测 |
| 查看 | 分页元数据检索、内容hash校验、独立审计角色、双人审批上下文、`llm-admin/audit`页面 | 普通LiteLLM管理员自动获得读取权限 |
| 留存 | 默认7天、过期拒绝读取、保全登记、分页清理与访问留痕 | 已部署自动清理或法定不可篡改保全 |
| Guardrail | 输入Content Safety、observe/block及故障策略 | Prompt Shields、PII、输出阻断及召回/误报验收完成 |
| Sentinel | 5个禁用规则、3个低风险响应契约 | 实际Playbook/连接器已运行 |

组合目标为`deploy/validation/stage8`，未进入环境overlay。L3、Guardrail、OTLP默认关闭，清理CronJob暂停，审批和保全列表为空。本轮未执行Azure What-if；此前阶段5结果不代表新增审计资源的变更计划。

## 2. L3采集与可靠性

服务端binding中的`audit.capture=true`选择采集对象，`audit.teamId`为可信Team映射。客户端不能选择审计租户或通过no-log关闭强制审计。

```text
认证/授权通过 -> 写l3-pending意图 -> 转发模型请求
             -> 有界响应缓冲 -> 写l3-content -> 写l3-index完成记录
             -> L1仅输出完成/部分/缺口元数据
```

意图失败、容量满或写入故障后的短暂熔断时，强制审计请求返回503，不调用上游。意图只含可信tenant、subject、Team、model、Trace、Request ID及时间，不包含正文。原文对象区分解析后的客户端请求、代理规范化转发请求和实际响应，不是HTTP字节级镜像。

单请求沿用1MiB输入限制；响应最多2MiB，每Pod最多16个采集/待写请求。SSE保存有序文本和chunk计数，只落一个最终对象，避免chunk与最终对象重复持久化。缺少终止信号、断连、解析失败和超限均标记为不完整；不支持的响应编码/类型不保存不可解释的二进制内容。审计请求要求上游使用identity编码。

同次采集的重复finish是幂等的。Azure写入使用稳定对象名及`If-None-Match:*`防覆盖，SDK最多3次重试、单对象15秒截止；结果不确定时保守记录缺口，不宣称跨进程exactly-once。

**重要限制**：响应写Blob前暂存在内存，尚无持久化队列。Pod崩溃可能丢失缓冲内容；持久化意图可在15分钟后被扫描为缺口，但不能恢复正文。当前不保证每个已发送SSE字节均已持久化，不能撤回已发送内容。若客户要求零缺口，必须进一步实现持久化队列/逐段写入并验证背压、容量与故障。

## 3. 原文与凭据

批准的业务原文（包括PII/源代码）与认证凭据分开处理。Authorization、Cookie、内部Key和完整Header不采集。已知结构化凭据字段、Bearer、Key/JWT和连接串模式会清除，记录`redactionCount`/`fidelity`，不将修改后的副本冒充未修改原文。

规则清除不是完整DLP，不能保证识别任意格式Secret。本轮没有PII匿名化或独立脱敏预览接口。索引只含必要元数据及内容SHA-256，普通日志和Trace不复制全文。

只记录实际经过网关的内容与tool定义/call/result；不读取终端文件、不补造隐藏Chain-of-Thought、不推断网关外工具执行。阶段7关闭的WebSocket、Files/MCP、对象引用和加密多轮上下文继续拒绝，不能直接切换现有Codex入口。

## 4. 查询、原文查看与审批

新增独立Entra/local role `audit_reader`，不能携带LiteLLM后端Key或模型权限。普通`proxy_admin`不能进入`/audit`，审计角色也不能调用原有LiteLLM管理API。

- `GET /audit`：受控查询查看页面；使用安全文本渲染、CSP、no-store，不使用localStorage或批量导出。
- `POST /audit/search`：审批范围内的元数据分页，可按subject/trace过滤。每页最多扫描50条，稀疏过滤可返回空页及下一页游标。
- `POST /audit/view`：明确record ID，经权限、到期时间和内容hash检查后返回内容。
- POST沿用管理Cookie、同源Origin和CSRF校验，不能用API Token或伪造身份Header代替。

审批由只读`/etc/audit-approvals/approvals.json`提供，每项包含：`id`、`actorOid`、`tenantId`、`ticketId`、`reason`、`teamIds`、`approvedBy`、`from/to`、`validFrom/validUntil`。服务端验证两个不同且非申请人的审批人、工单理由、租户/主体/Team、最多7天查询范围及有效期。

审批配置发布者是信任边界。这不是工单系统签名或PIM自动激活：须独立双人工作流发布，申请人与普通管理员不能修改ConfigMap、exec代理或取得其存储身份。admin进程拥有Blob读权限，进程被攻陷仍可能绕过应用审批；更强隔离需独立审计读服务/身份。

查询/查看attempt与release写入`l3-access`；留痕失败拒绝返回原文并输出`l3_access_log_failed`。released表示服务端准许返回，不证明浏览器完整接收。已验证正文HTML仅显示为文本、桌面和390px窄屏可用、无效审批403且详情清空。

## 5. Azure存储与留存

`infra/audit-storage`是独立默认关闭入口，不重构备份存储。新StorageV2采用Standard_LRS、TLS1.2、HTTPS、公网关闭、Shared Key关闭、CMK和Infrastructure Encryption、Blob PE、现有DNS/子网引用及访问诊断。

容器为`l3-content`、`l3-index`、`l3-pending`、`l3-access`。写身份仅有前三个write；读身份仅有content/index read及access write；清理身份仅有前三个read/delete、access write和Blob service策略只读。账户CanNotDelete锁不替代数据访问授权。

CMK使用专用UAMI及已存在Vault/Key的Key级Crypto Service Encryption User。CMK Vault必须允许Azure Storage服务访问并验证网络/轮换，不能假定创建PE就能取密钥。UAMI/Federation、资源、RBAC和配额仍需云端验证。SDK只用WorkloadIdentityCredential，首期仅支持Azure公有云endpoint。

默认留存7天，可配置1至30天。到期在查询时立即不可见，物理清理由暂停的`l3-retention`独立SA CronJob每15分钟执行；实际清理时间受调度和扫描影响。清理使用ARM验证版本、Blob软删除、容器软删除均明确关闭，未知配置拒绝删除；不能用属性不完整的Blob SDK数据面验证替代ARM。

清理按content、index、pending顺序删除并留痕；每页1000条、Job最多100页且10分钟截止。截断/超限/失败告警，不静默漏扫。账户历史版本、外部备份和复制须单独盘点，关闭版本功能不等于历史版本已删除。

`holds.json`每项包含tenantId、record id、caseId和until。有效hold阻止清理，无效登记拒绝清理，但hold不自动授予过期原文读取权。当前是应用层保全，不是Immutable Storage法定锁；Job期间新hold与删除的竞态仍需进一步解决。未配置盲目生命周期删除以免绕过hold。`l3-access`本轮不自动删除，其期限和不可变性必须由客户单独批准，不能无限期默认保留。

## 6. 遥测、Guardrail与检测

新Trace由服务端创建并传给LiteLLM，返回X-Trace-ID/X-Request-ID。仅手动gateway Span，不启用正文/数据库自动采集。Collector属性白名单、禁用span events，使用Entra Workload Identity导出Azure Monitor。

Collector配置已在contrib 0.151.0固定digest `sha256:d57bfe8eee2378f31cb1193239fbcac521d54a5a071fca2bfc106916a32b892d`容器离线解析通过，使用发布版的`azuremonitor`名称。部署镜像仍须ACR扫描、SBOM/签名；Collector身份权限、App Insights配置和Firewall出口需审批及实测。

LiteLLM/模型/工具Span关联尚未逐层验收，Container Insights DCR/DCRA和Managed Prometheus未新增部署模块。Sentinel规则依赖的ContainerLogV2仍需新AKS接入；本地stdout不等于Monitor已收到数据。

Content Safety使用`2024-09-01`文本API及Entra Token。输入上限10000 Unicode code points；超限/服务错误时block模式拒绝，observe模式记录缺口。中英文合成provider测试不证明云服务准确率。PII、Prompt Shields、输出阻断与工具动作控制仍待后续工作；保留Foundry原生过滤。

`infra/audit-detection`提供认证异常、L3缺口、部分/清理失败、Content Safety、读取拒绝等5条Scheduled规则，创建和enabled均默认false。通知、调查工单、incident标签仅有3个禁用响应契约，尚未实现客户Logic Apps连接器或自动操作。

## 7. 使用与合成演示

```bash
npm ci --prefix auth-proxy --ignore-scripts
make validate-stage8
node auth-proxy/test/stage8-demo.mjs
./.venv/bin/python scripts/render_stage7_domain.py --stage 8
kubectl kustomize temp/stage8-domain
```

客户主域仍是本地/环境参数，生成器不会启用审计。开启需同时批准L3开关、真实Blob URL/ARM ID、采集binding.audit、独立审批和hold配置；必审计binding在L3服务关闭时拒绝请求，不静默漏记。

`stage8-demo.mjs`通过本地真实HTTP代理和内存存储演示：推理200 -> 索引 -> 获批原文200 -> 无效审批403 -> 过期删除。认证为合成值，不是真实Entra测试。

`node auth-proxy/test/viewer-preview.mjs`仅在127.0.0.1:4188展示固定合成数据。Approval ID为`11111111-1111-4111-8111-111111111111`；不访问Azure/Entra/生产，测试文件不进入生产镜像。

## 8. 发布前Gate

1. 客户L3目的、范围、告知、审批、分类、留存和保全批准；
2. 新资源What-if和CMK、PE/DNS、RBAC、Workload Identity真实读写/拒绝；
3. Blob条件写、重试、分页、历史副本清除及故障恢复；
4. 真实租户/Team/审批/吊销、跨Pod与配置传播验收；
5. 容量、成本、崩溃缺口和排空测试；零缺口要求需持久化队列；
6. 独立审批发布权限、PIM/工单验证、不可变访问留痕和保全竞态处理；
7. 所需协议授权和所有权检查通过后才开放WebSocket/Files/MCP/加密多轮；
8. Collector、Container Insights、Prometheus、真实Content Safety及Sentinel/响应联动；
9. 容器扫描、SBOM/签名、依赖复核与所有占位符替换；
10. 维护窗口和回退审批后才部署或启用生产采集。

本轮交付L3首期可执行闭环，不代表阶段8全部生产门槛已通过。

## 9. 本地验证结果

- `make validate-stage8`通过：阶段3至7回归、全部Bicep/参数编译、阶段8组合清单和产品边界；
- Node测试28项通过；既有Python基线13项、阶段7清单/域名10项、阶段8清单3项通过；
- 合成HTTP演示通过：推理200、获批原文查看200、无效审批403、过期删除1条；
- 最终本地镜像以UID10001、read-only、drop ALL、无外网运行，全部28项测试及演示通过；
- Collector 0.151.0固定digest实际配置解析通过；
- npm audit未发现已知漏洞，GitHub Actions语法、差异空白及相关编辑器诊断通过；
- Playwright桌面/窄屏检索查看、错误审批和正文HTML文本渲染检查通过；
- 未执行云端部署、真实Entra/Blob/Content Safety调用、Sentinel规则执行、镜像推送或Git提交。