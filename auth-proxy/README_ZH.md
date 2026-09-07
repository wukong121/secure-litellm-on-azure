# Entra认证代理

## 阶段9回源补充检查

Stage9 API Deployment显式注入`FRONT_DOOR_ID`，启动时必须是有效GUID；业务请求的`X-Azure-FDID`须匹配，重复/缺失/错误值拒绝，之后仍执行Entra认证和路由授权。admin代理不允许配置该参数。此前Stage7/8未设置时行为保持不变。内部健康探针仍可用，但Front Door公网路由不包含探针路径。

FDID不是Secret，不能替代PLS连接审批、API-only私有LB和NSG源站防绕过。部署流程必须保留这一环境变量，不能通过删除它绕过检查。详见[阶段9准备记录](../docs/litellm-stage9-edge-cutover-preparation-2026-09-07.md)。

## 阶段8新增能力

L3首期实现包括按身份选择采集、私有Blob适配器、有界JSON/SSE响应、元数据索引、独立`audit_reader`审批查看、`/audit`页面和留存/保全清理。默认关闭，不改变阶段7已关闭协议。详见[阶段8实施记录](../docs/litellm-stage8-l3-audit-observability-2026-09-07.md)。

执行`make validate-stage8`运行回归，`node auth-proxy/test/stage8-demo.mjs`运行内存存储合成HTTP闭环。设置`STAGE8_CONFIG=/etc/stage8/config.json`接入配置；生产仅用Workload Identity，不使用连接密钥。必审计binding设置`audit: {capture: true, teamId: "<受控Team>"}`，服务不可用则拒绝请求。独立审计binding为`plane=admin, role=audit_reader, models=[]`且不配置keyFile。

原文查询与查看为同源CSRF保护的`POST /audit/search`和`POST /audit/view`，均需approvalId，view另需record id。`GET /audit`提供页面；普通proxy_admin不能进入。审批和hold配置必须由独立审批工作流发布，不能由申请人修改。

内容暂存内存，写入失败/Pod崩溃可能形成显式缺口；不是持久化队列或零丢失保证。原文清除前使用ARM核对版本/软删除，部署和真实存储测试仍未执行。

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

主域名是环境参数，不在公共代码中固定为某个客户域。当前本地值在Git忽略的`auth-proxy/domain.local.json`中；执行`./.venv/bin/python scripts/render_stage7_domain.py`生成`temp/stage7-domain`，再通过`kubectl kustomize temp/stage7-domain`预览。客户可传入`--base-domain <客户主域名>`或自己的`--config`文件。

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