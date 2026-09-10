# LiteLLM安全增强阶段7：Entra认证代理与双子域隔离

> 状态：第一轮代码、离线测试和组合清单完成；未部署、未切流、未提交  
> 日期：2026-09-07  
> 产品边界：仅Microsoft Entra、客户自有代码和OSS依赖，不引入LiteLLM Enterprise或APIM  
> 运行时验收仍受阶段4/5新环境和客户租户权限阻塞

## 2026-09-10 契约变更

以下第1至各节保留2026-09-07历史设计和验证记录，不作为当前API凭据接入指导。当前API已改为“企业Token + 客户端vkey”：企业Token在Authorization，vkey在X-LiteLLM-API-Key；两者缺一拒绝。代理只做企业准入，不再维护API模型ACL、映射内部Key或挂载API CSI凭据；LiteLLM统一决定模型和预算。管理OIDC/CSRF及管理凭据不变。当前Static Stage7只有管理代理的认证SecretProviderClass，后台自身CSI不在此计数中。

新契约、旧配置迁移与仍未完成的客户端验收见[当前代理说明](../auth-proxy/README_ZH.md)。旧API Vault、Key、角色及CSI对象不因本次发布自动删除或吊销；禁止借历史已通过测试宣称新方案已完成真实Entra/客户端/模型验收。

## 1. 子域冻结

| 入口 | 用途 | 认证 | 网络 |
| --- | --- | --- | --- |
| `llm-api.<客户域名>` | 批准的推理API | Entra access token，逐身份映射内部Virtual Key | Front Door/WAF经Private Link到独立私有API ingress，阶段9完成入口建设 |
| `llm-admin.<客户域名>` | 管理OIDC登录和批准的管理API | 独立OIDC Code + PKCE、App Role、短会话和CSRF | 独立私有admin ingress与内部DNS，不配置公网数据面路由 |

当前验证环境的主域名已确认，保存在被Git忽略的`auth-proxy/domain.local.json`的`baseDomain`中。客户环境通过自己的`baseDomain`参数替换，不与当前验证域名绑定。公共模板继续使用`llm-api.example.com`和`llm-admin.example.com`，避免把个人环境写入通用方案。

运行以下命令会从本地配置生成`temp/stage7-domain`中的Kustomize overlay、代理策略及管理App Registration模板，统一更新Ingress Host、TLS hosts和OIDC回调：

```bash
./.venv/bin/python scripts/render_stage7_domain.py
kubectl kustomize temp/stage7-domain
```

客户可用`--base-domain <客户主域名>`，或`--config <客户本地JSON配置>`覆盖输入；可用`--output-dir temp/<环境目录>`隔离不同环境。生成目录被Git忽略，不修改公共模板，不创建DNS记录或证书，不执行Azure/Entra/Kubernetes部署。

仅生成域名不代表配置可部署：租户、客户端ID、IngressClass、镜像和凭据等占位符仍须补齐。代理仍会拒绝包含未替换占位符的配置启动。

两个域名不能只指向同一未受保护的LiteLLM入口。Stage7分别定义2个代理Deployment、2个ServiceAccount、2个Service、2个PDB和2个CSI SecretProviderClass；各代理仅接受本plane的Host。

API ingress controller位于`llm-api-ingress`，admin controller位于`llm-admin-ingress`，使用不同IngressClass。控制器选择、私有LoadBalancer、TLS证书、Private Link、内部DNS和HTTP到HTTPS跳转均为部署Gate，不能因为存在Ingress YAML就宣称入口已私有化。

## 2. 已实现的信任链

```text
llm-api -> 独立API代理 -> Entra JWT验签和客户端/身份白名单
        -> 路由、JSON字段、模型限制 -> 用户专属内部Key -> LiteLLM

llm-admin -> 独立管理代理 -> Entra OIDC + App Role + 本地身份映射
          -> 加密短会话 + 管理写CSRF -> 管理身份专属后端凭据 -> LiteLLM
```

实现路径：`auth-proxy/`。Node 24依赖`jose`、`openid-client`和`http-proxy-middleware`，不手写JWT签名算法或OIDC协议交换，不激活LiteLLM付费认证。

访问控制：

- 只接受单一Entra租户和精确API audience；RS256、exp、nbf、iat及客户端azp校验；
- delegated scope `llm.invoke`，或应用token的`idtyp=app`与`Llm.Invoke`角色；
- 每个主体必须有显式oid映射，空映射、禁用身份、错误租户/客户端、未知模型均拒绝；
- Admin OIDC使用独立client ID、固定回调、state、nonce和PKCE；
- 代理自身实现`internal_user`、`proxy_admin_viewer`、`proxy_admin`的许可，不依赖LiteLLM Enterprise RBAC；
- Viewer不允许管理写；管理员写需要同源Origin和CSRF Token；
- 不共享身份Key，无Master Key兜底，不把Entra token透传给LiteLLM；
- 剥离外来身份、Team、内部凭据、Cookie和路由Header，限制请求字段，避免调用者覆盖upstream认证；
- 未知路径、编码/大小写/尾斜杠变体、错误方法和未批准查询参数均拒绝；
- 依赖不可用或密钥缺失返回拒绝/503，上游不可用返回通用502，不暴露异常中的敏感值。

模型权限和预算仍需由LiteLLM OSS内部Key对应的用户/Team配置执行。代理的模型白名单是补充控制，不能替代实际后端Key权限验收。管理read/write凭据是否能在候选OSS版本实现所需最小权限必须实测，不可改用共享Master Key解决。

## 3. 网络和Secret边界

组合目标为`deploy/validation/stage7`，包含阶段4/5/6和阶段7组件，未加入dev/test/prod overlay。

Stage7替换原先ingress直接访问LiteLLM 4000的许可，只允许同namespace的认证代理Pod。API与admin代理各自只接受对应controller namespace和Pod标签；后端保持ClusterIP。

两代理出口只允许CoreDNS、LiteLLM和受Azure Firewall进一步限制的HTTPS，排除RFC1918及link-local/IMDS。Pod标签不是密码学身份，因此namespace中创建/修改Pod、ServiceAccount、NetworkPolicy和exec权限必须严格限制，不能授予普通用户。

两套认证凭据使用独立Key Vault和独立UAMI，避免API身份读取admin凭据或LiteLLM Master/Salt。模板只有名称占位符，本轮没有新增Vault/UAMI资源。部署时复用阶段4的`workload-identity`模块及阶段5的`key-vault`、`key-vault-private-endpoint`模块，分别绑定：

| SA subject | Vault权限 | CSI内容 |
| --- | --- | --- |
| `system:serviceaccount:litellm:llm-api-proxy` | API认证专用Vault的Secrets User | 每个API主体的内部Virtual Key |
| `system:serviceaccount:litellm:llm-admin-proxy` | 管理认证专用Vault的Secrets User | 每个管理主体的后端凭据、OIDC client secret、会话加密key |

上述新Vault需Private Endpoint、共享Vault DNS Zone和删除保护；不得给代理分配读取LiteLLM原有Vault的角色。受控执行路径将真实值直接写入Key Vault；没有任何凭据通过Git、环境参数或部署输出传递。

代理使用CSI文件，不同步Kubernetes Secret，不使用envFrom。新增SecretProviderClass不含`secretObjects`。原阶段5 LiteLLM三个环境变量的Secret同步仍是其既有补偿方案，没有被误称为已移除。

Stage7还删除LiteLLM的`AZURE_CLIENT_ID`旧Secret引用，依赖已配置的Workload Identity webhook注入，避免引用阶段5并未同步的字段。

## 4. 协议矩阵

| 协议或功能 | 第一轮状态 |
| --- | --- |
| Chat Completions / Embeddings HTTP | 已实现授权与转发；真实模型待验收 |
| Responses HTTP / SSE | 支持无对象引用的请求；本地验证首事件不等待上游结束 |
| 管理OIDC | 登录请求/PKCE与加密会话已有实现和本地测试；真实回调换码、App Roles和CA待租户验收 |
| 管理GET | 仅`/model/info`、`/team/info`、`/key/info`，后端权限待验收 |
| 管理POST | 仅管理员`/key/block`、`/key/unblock`，要求CSRF |
| 原生LiteLLM UI/SSO/本地登录 | 默认拒绝；尚未实现OSS原生UI会话桥接，OIDC不能冒充原生UI无感登录 |
| WebSocket、Files、MCP、其他管理操作 | 默认拒绝，等待逐协议授权证明 |
| previous_response_id、item_reference、加密上下文、file_id、store=true | 默认拒绝，尚无跨请求对象所有权索引 |

不能把该组合直接切换到现有Codex/Agent入口。已运行的WebSocket和加密多轮流程会被这些限制拒绝；阶段6路由能力并未因此得到阶段7端到端授权证明。安全扩展应先建立tenant/oid到response/file对象的所有权索引，再放行相应协议。

管理面当前是OIDC保护的有限管理API，不是完整的LiteLLM Admin UI。`/fallback/login`不会在代理中打开。Break-glass需私网运维通道、PIM/双人批准、限时操作和外部审计；本轮没有创建永久豁免路由或自动授予应急权限。

## 5. 生命周期与回收

- 管理Cookie：JWE加密，Secure/HttpOnly/SameSite=Lax，`__Host-`且无Domain，绝对TTL最多5分钟，不保存access/refresh token；
- API：每请求验签和映射校验，token年龄上限90分钟、30秒时钟容差；已签发token不因Entra账号刚被禁用而即时失效；
- 紧急回收：禁用身份映射，验证各Pod的ConfigMap传播，再吊销后端Key；这不是已实现的Graph实时吊销检测；
- 内部Key文件每请求重读；CSI有传播间隔，后端旧Key吊销也需检查LiteLLM缓存失效；
- OIDC secret和共享会话key启动时加载，轮换需滚动重启；session key轮换会使旧会话失效；
- 租户、audience和域名信任配置变化要求重启；缺失或无效配置使readyz失败；
- 单请求上限570秒；preStop 30秒和600秒Pod退出预算不代表长连接无损滚动。

## 6. Entra准备

`auth-proxy/entra/`提供单租户App Registration模板：API v2 access token + scope/application role；admin Web redirect + 管理App Roles，关闭implicit flow。

客户目录管理员须创建对应Service Principal，启用`appRoleAssignmentRequired`、分配角色和管理员同意；API客户端ID白名单与逐主体映射同时配置。管理员角色使用受控安全组并结合PIM/Conditional Access/MFA。模板本身不会执行上述Graph操作，也不证明Microsoft Entra许可证和策略已经到位。

OIDC登录成功不证明已经强制MFA。需要实际Conditional Access策略、登录日志、普通用户和未分配账号负向测试。

## 7. 验证与Gate

统一入口：`npm ci --prefix auth-proxy --ignore-scripts`后执行`make validate-stage7`。

已执行的本地检查：13项Node策略/JWT/会话/代理测试；5项清单正负向测试；依赖audit；固定Node镜像构建；UID10001、只读文件系统、无外网容器内全部13项代理测试。统一Gate还包含Stage3至Stage6回归、Bicep编译、组合渲染和OSS付费能力排除检查。

依赖audit不等于容器OS漏洞扫描。本轮镜像仅保留本地测试tag，没有推送ACR、签名、部署或提交。没有修改Azure IaC资源，因此未新增Azure What-if；此前Stage5结果不能当作新增代理Vault/UAMI的What-if证明。

发布前仍需：

1. 创建并验证独立Vault/UAMI/Federation/RBAC及CSI私网路径，执行新增资源What-if；
2. 冻结客户主域、证书、两个私有controller和admin内部DNS，确保管理Host不能从API入口到达；
3. 真实OIDC成功/失败、nonce/state/code重放、JWKS轮换、Token/会话回收及MFA/CA测试；
4. 后端逐身份Key、Team、模型、预算、管理员/Viewer最小权限与即时吊销测试；
5. 补齐需要的UI和多轮协议所有权授权，或明确不支持，不能透明放行；
6. HPA/容量、登录限流、SSRF、请求体上限、SSE异常、节点drain与长连接压测；
7. 代理日志接入Monitor/Sentinel，Break-glass外部告警和事故Runbook；
8. ACR漏洞扫描、SBOM/签名及digest更新，审查所有占位符和映射，不使用Master Key兜底。

阶段7当前是可运行、可离线验证的受限第一轮实现，不是完整生产授权验收。