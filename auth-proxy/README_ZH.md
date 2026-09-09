# LiteLLM客户自有Entra认证与审计代理

本组件属于[安全增强版LiteLLM on Azure](../README_ZH.md)。客户部署入口为[分阶段迁移指南](../docs/customer-migration-guide-zh.md)，从受保护Environment注入配置；不能把本地合成测试通过当作生产身份、协议和审计验收。

## 阶段9回源补充检查

Stage9 API Deployment显式注入`FRONT_DOOR_ID`，启动时必须是有效GUID；业务请求的`X-Azure-FDID`须匹配，重复/缺失/错误值拒绝，之后仍执行Entra认证和路由授权。admin代理不允许配置该参数。此前Stage7/8未设置时行为保持不变。内部健康探针仍可用，但Front Door公网路由不包含探针路径。

FDID不是Secret，不能替代PLS连接审批、API-only私有LB和NSG源站防绕过。部署流程必须保留这一环境变量，不能通过删除它绕过检查。详见[阶段9准备记录](../docs/litellm-stage9-edge-cutover-preparation-2026-09-07.md)。

## 阶段8新增能力

L3首期实现包括按身份选择采集、私有Blob适配器、有界JSON/SSE响应、元数据索引、独立`audit_reader`审批查看、`/audit`页面和留存/保全清理。默认关闭，不改变阶段7已关闭协议。详见[阶段8实施记录](../docs/litellm-stage8-l3-audit-observability-2026-09-07.md)。

执行`make validate-stage8`运行回归，`node auth-proxy/test/stage8-demo.mjs`运行内存存储合成HTTP闭环。设置`STAGE8_CONFIG=/etc/stage8/config.json`接入配置；生产仅用Workload Identity，不使用连接密钥。必审计binding设置`audit: {capture: true, teamId: "<受控Team>"}`，服务不可用则拒绝请求。独立审计binding为`plane=admin, role=audit_reader, models=[]`且不配置keyFile。

原文查询与查看为同源CSRF保护的`POST /audit/search`和`POST /audit/view`，均需approvalId，view另需record id。`GET /audit`提供页面；普通proxy_admin不能进入。审批和hold配置必须由独立审批工作流发布，不能由申请人修改。

未设置`l3.deliveryMode`或设置为`buffered`时，沿用内容暂存内存的模式，写入失败/Pod崩溃可能形成显式缺口。`persist-before-forward`模式仍需显式选择；静态模板默认关闭，配置auditRuntime后的Stage8生成器按客户决策启用。两种模式都不是零丢失保证，原文清除前使用ARM核对版本/软删除，真实Azure存储与身份测试仍未执行。

Stage8生成器保留API/admin的ServiceAccount主Client ID与原CSI凭据挂载，另在各自审计配置中填入独立writer/reader的l3.clientId；SDK显式选择此身份，不借用凭据Vault身份访问Blob。durable模式缺少Client ID或复用主身份会拒绝启动。留存作业使用独立配置与l3-retention身份。已有审批/保全ConfigMap不被应用发布覆盖，首次创建为空，默认无原文查看批准。collector自动接线尚未完成，审计专用发布不能覆盖已有启用的telemetry/guardrail。

### 持久化前置交付与恢复核心

- API请求在调用上游前保存受信上下文和脱敏请求。SSE仅在完整事件组的日志分片得到存储确认后转发，JSON在完整正文、结束标记和索引提交后转发；存储失败时不释放尚未记录的正文。已开始的流会中断，尚未发送响应时返回通用503。
- 使用`eventsource-parser`解析事件并规范化SSE；注释心跳、retry和独立id字段不透传。必须使用有效UTF-8、未压缩JSON/SSE，SSE末尾须为完整空行边界。默认每请求2MiB、最多1024个分片、同时16个请求；超限拒绝继续输出，不是无界队列。每个分片增加Blob事务和往返延迟，需客户批准并实测成本/延迟预算。
- 分片包含连续序号和前序哈希，结束标记绑定分片数量及末尾哈希；写入确认不确定时不生成矛盾结束标记。哈希链用于一致性检查，不是独立签名或针对可改写全部对象的管理员的防篡改保证。
- `complete`仅表示已持久化内容的传输捕获终态；`modelOutcome`单独保留，`clientDelivery`始终为`unconfirmed`。`observedBytes`统计已保存分片脱敏前的规范化文本字节，不代表全部网络字节或尚未记录的数据。正文是脱敏/规范化版本，不是逐字节原始网络包；跨多个SSE事件拆开的凭据仍需额外脱敏设计，不能承诺任意秘密均能被检测。
- `audit-recovery.mjs`可检查超过15分钟且未过期的请求、验证上下文/序号/哈希链、补索引或重建正文。缺少结束标记时仅恢复部分记录，不能因为看到`[DONE]`就推断进程完成或客户端收到。活动/过期记录不恢复；有保全的过期记录也不会因此重新开放原文读取。
- `audit-recovery-job.mjs`保留内部有界计划/执行接口，`audit-recovery-worker.mjs`由专用Kubernetes Job调用。客户workflow通过audit-pause、audit-recover、audit-resume管理持久检查点，审批使用成功计划运行编号自动取artifact；每批最多25个请求，绑定代码、配置、镜像、存储、检查点和日志状态，只返回元数据。Job独立核验API副本为零、留存暂停且无活动写入Pod/Job，逐条修复前复核。15分钟年龄门槛不能替代暂停证明。
- 独立恢复身份及RBAC已加入模板：显式audit.recoveryPrincipalId=auto后重新部署foundation及audit，恢复身份对content/index读写、pending只读、access只写，无删除权。现有API writer仍然只写，不能借用管理员reader或retention身份。Blob重复提交仅在有读取权限且原对象逐字节相同时认定幂等；只写身份遇到无法核对的重复提交会拒绝，而不会扩大权限或覆盖对象。
- 留存删除包含请求、正文、索引和全部日志分片；保全覆盖这些副本。删除中途失败保留pending标记，下一次可继续清理。恢复必须在有检查点的停机窗口进行，失败后不自动恢复流量；普通application发布在窗口中被阻断。外部GitOps、额外写入程序和管理员操作仍须冻结，这不是分布式锁。存在API HPA或额外写入控制器时拒绝操作。

本地测试覆盖真实HTTP存储确认顺序、失败拒绝、UTF-8/CRLF分片、未结束SSE尾部、结构化凭据字段脱敏、丢失写入确认、分页保全/删除续跑，以及子进程将合成日志同步写入临时磁盘后被SIGKILL的恢复。恢复workflow另有中断状态机、artifact审批、实际kubectl到本机HTTP的UID删除前置条件及Python/Node检查点哈希测试。Stage8审计清单另有身份/联邦/私网输出核验、CSI保持、治理登记保留、开关滚动更新及plan/execute模拟组合测试。磁盘测试不是Azure Blob故障证明；模型侧可信回调接收器、实际托管身份/私网/RBAC/CNI、跨事件脱敏、治理发布和collector仍未完成。流程和停机/授权要求见[客户部署指南](../docs/customer-deployment-workflows-zh.md#审计恢复维护窗口)。

客户自有Node 24组件，使用`jose`验证JWT、`openid-client`完成OIDC Code + PKCE、`http-proxy-middleware`转发HTTP/SSE。不调用LiteLLM原生JWT、Enterprise RBAC或付费SSO。

本组件是阶段7第一轮实现，不是已经部署的服务。尚未进行真实Entra租户、LiteLLM授权、入口或负载验收。

## 本地验证

```bash
npm ci --prefix auth-proxy --ignore-scripts
npm test --prefix auth-proxy
npm audit --prefix auth-proxy --omit=dev --audit-level=high
make validate-stage7
docker build -t litellm-entra-auth-proxy:stage7-check auth-proxy
```

依赖使用精确版本和lockfile；基础Node镜像固定digest。发布前仍需ACR导入、镜像漏洞扫描、SBOM和签名，不得用本地测试tag替换部署模板的批准digest占位符。

## 配置契约

主域名由客户Environment的`CUSTOMER_CONFIG_JSON.baseDomain`提供，不固定为某个个人或客户域。本地单独预览时运行`./.venv/bin/python scripts/render_stage7_domain.py --base-domain <客户主域名>`生成`temp/stage7-domain`，再用`kubectl kustomize temp/stage7-domain`查看。也可显式传入自己的忽略配置文件；客户workflow不依赖个人本地文件。

生成器同时更新代理Host、Ingress Host/TLS及管理OIDC回调，保留其他安全配置和占位符。ConfigMap使用Kustomize内容hash，域名变化会更新Deployment引用。生成只在忽略的`temp/`子目录中进行，不修改模板、不部署；后续受控发布流程还需补齐身份、凭据、IngressClass和镜像参数。

`/etc/auth-proxy/policy.json`来自受控ConfigMap：

- `tenantId`：唯一受信Entra租户；issuer/JWKS地址从固定Microsoft登录域名构造，禁止`common`；
- `apiHost`、`adminHost`：`llm-api.<域>`与`llm-admin.<相同域>`；
- `apiAudience`：数据面API的精确access-token audience，与admin OIDC client ID分开；
- `apiClientIds`：允许调用API的客户端App ID白名单；
- `adminClientId`：独立管理OIDC应用；
- `bindings`：默认空，每项有`plane`、`oid`、`role`、`keyFile`、`models`，可设置`disabled=true`立即在配置传播后拒绝该身份。

API映射role只能为`internal_user`；管理映射只能为`proxy_admin`或`proxy_admin_viewer`。API token同时需要`llm.invoke` delegated scope，或`idtyp=app`与`Llm.Invoke` application role。管理角色从经OIDC验证的ID Token读取并与显式映射相交，不接受客户端Header声明。

`keyFile`只能是CSI挂载目录下的简单文件名，每个身份独立，禁止默认用户、通配模型或共享Key。每个文件保存一个由独立受控流程预先创建的LiteLLM内部凭据；它必须绑定正确的用户/Team、模型权限和预算。代理不创建Key、不使用Master Key兜底，不自动将客户端Team字段映射为授权。

凭据挂载目录为`/mnt/auth-secrets`；API代理只能挂载API凭据。管理代理另需`oidc-client-secret`和`session-key`，后者为32随机字节的base64编码，两个管理副本共享。实际值不得进入Git、环境参数、命令历史或日志。

内部凭据每请求重读，可在CSI轮换传播后生效。OIDC client secret及会话加密key在启动时加载，轮换必须滚动重启；会话key轮换会使旧Cookie失效。身份绑定/禁用每请求重读，ConfigMap必须整目录挂载、不使用subPath；tenant、audience或域名变化会拒绝请求并要求重启。

`REPLACE_*`、`example.com`会使程序启动失败。模板不可直接应用。`PROXY_PLANE=api|admin`是唯一运行模式选择，后端固定为同namespace的LiteLLM ClusterIP，不接受请求控制的上游地址。

## 支持边界

- `llm-api`：仅POST Chat Completions、Responses、Embeddings，支持HTTP/SSE。无CORS配置，首版面向CLI和服务端客户端。
- `llm-admin`：根路径跳转到`GET /auth/login`启动OIDC，回调为`/auth/callback`；`GET /auth/session`返回角色和CSRF值；`POST /auth/logout`结束本地会话。
- 管理读：`GET /model/info`、`/team/info`、`/key/info`；后端身份仍需相应权限。
- 管理写：仅`proxy_admin`可POST `/key/block`、`/key/unblock`，必须同源Origin和正确`X-CSRF-Token`；body只接受`key`字段。
- 关闭：原生`/ui`、`/login`、`/sso`、`/fallback/login`、Key创建、模型写配置、WebSocket、Files、MCP、Responses对象读取/删除、`previous_response_id`、item references、文件引用、加密上下文和`store=true`。

关闭的路径返回拒绝，不会透明绕过认证。尤其不能把本组件替换到当前Codex/Agent生产入口：现有WebSocket与加密多轮上下文会被拒绝。管理OIDC完成不等于LiteLLM原生UI无感登录完成；后续UI桥接必须验证OSS授权与Cookie边界。

请求JSON上限1MiB，只允许显式字段，禁用上游凭据/URL/metadata/header覆盖。`user`重写为tenant+oid的哈希，仅用于归因，不替代后端授权。默认剥离客户端Cookie、内部Key、身份、路由与转发Header；响应不透传内部Set-Cookie或Location。日志只输出代理request ID、plane、伪名化subject及状态码，不输出Token、body、query、凭据或异常细节。

## 安全验收

管理Cookie使用加密JWE、Secure/HttpOnly/SameSite=Lax、无Domain的`__Host-`名称，绝对有效期最多5分钟。API接受RS256/v2 token，验证issuer/audience/signature/exp/nbf，最多90分钟token年龄、30秒时钟偏差。离职/禁用Entra账号不会即时撤销已签发token；紧急阻断应禁用映射并验证ConfigMap传播，管理Cookie也存在有效期内的撤销延迟。

单请求总时长上限570秒；30秒preStop和600秒Kubernetes退出窗口仅提供排空预算，不证明长连接无损发布。没有分布式登录限流或全局会话吊销存储；JWKS轮换、OIDC回调成功、会话重放/撤销、CSRF、后端权限与流式异常必须在隔离环境进一步验收。