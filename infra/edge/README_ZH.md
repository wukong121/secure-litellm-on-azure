# Stage 9边缘入口

独立资源组级Bicep入口，不自动纳入阶段4/5编排，不部署Kubernetes。默认`deployEdge=false`、`enableApiTraffic=false`、`enableAdminTraffic=false`、WAF Detection。

`main.bicep`创建一个Front Door Premium profile，但API和Admin使用不同endpoint、自定义域、origin group、Private Link origin、route和WAF policy。API route仍只有6个精确推理路径；Admin route为完整UI路径，但`llm-admin`自定义域强制`ClientCertificateRequiredAndValidated`，并在内层继续执行Entra或LiteLLM原生登录。两条route均无缓存、禁用默认`azurefd.net`域路由且仅支持HTTPS。

Admin mTLS使用`Microsoft.Cdn@2026-08-01-preview`；API及其稳定资源继续使用稳定API版本。客户必须在变更批准中明确接受Preview状态。若生产政策不允许Preview，应停止本路径并改用Application Gateway WAF v2严格mTLS，不得降级为公网密码加WAF。

edge plan会读取客户订阅的`Microsoft.Cdn` provider元数据，要求provider已Registered，且`profiles/customdomains`与`profiles/secrets`都公布`2026-08-01-preview`。未公布时停止，不用`az provider register`或模板API版本替换来盲试；由订阅Owner/Microsoft确认区域与订阅可用性后重新plan。

## 依赖顺序

1. 在新Private AKS建设经过支持周期/安全评审的两个独立私有ingress controller，分别使用`llm-api-ingress`和`llm-admin-ingress` namespace；本模块不安装controller。
2. controller owner创建API和Admin两个不同的Standard internal LB frontend，HTTPS证书分别覆盖`llm-api.<baseDomain>`和`llm-admin.<baseDomain>`；配置SSE不缓冲及适当超时。
3. `../edge-origin/main.bicep`分别引用两个已审查frontend并创建`pls-llm-api`和`pls-llm-admin`。NAT subnet需`privateLinkServiceNetworkPolicies=Disabled`，Stage4的`snet-private-ingress`已有此目标设置。
4. 将两个PLS ID/location输入本模块；部署edge后、执行edge-bind前，两条Front Door托管Private Endpoint请求都必须手工批准并核对profile、origin及目标frontend，不能只凭`requestMessage`。edge-bind会读取live origin和PLS，要求每条PLS恰有一个Approved且没有Pending连接。
5. 客户PKI把受信任客户端CA链的**公钥证书**存入专用Key Vault Secret，不得包含CA私钥或客户端私钥。配置Vault资源组/名称、Secret名称、固定版本及客户端证书SAN中的获准FQDN。模板为Front Door profile系统托管身份在该Vault授予`Key Vault Secrets User`。该同订阅Vault必须启用RBAC并设置`networkAcls.bypass=AzureServices`；`publicNetworkAccess`可为Disabled，因为Front Door Standard/Premium属于Trusted Services。Network Security Perimeter会覆盖此bypass，当前自动化拒绝该模式。RBAC成功不等于数据面可达，仍须在Front Door Secrets状态中核验。
6. 完成两个域的`_dnsauth`所有权、边缘托管证书、两份源站证书、客户端证书链/EKU/FQDN及吊销检查后，再评审启用流量。Microsoft托管边缘证书不安装到ingress。

PLS的`visibility=['*']`允许Front Door跨订阅发现，不代表数据访问授权；`autoApproval=[]`，不得自动批准未知连接。PLS绑定frontend只靠ID不能证明平面归属，必须核验LB后端池、rule、端口、selector与controller所有权。API和Admin不得引用同一PLS或同一frontend，也不得把旧公网gateway放入origin。

## Admin mTLS参数

| 字段 | 标识内容 | 如何取得与验证 |
| --- | --- | --- |
| `keyVaultResourceGroupName` | 存放客户端CA链Secret的同订阅Vault所在资源组 | Portal中打开Vault查看Resource group，或运行`az keyvault show --name <vault> --query resourceGroup -o tsv`；应与批准范围一致 |
| `keyVaultName` | 边缘信任Vault名称 | `az keyvault show --name <vault> --query name -o tsv`；这不是AKS后台业务Secret值 |
| `trustedClientCaSecrets[].secretName` | 仅含根/中间CA公钥链的Secret名称 | 由客户PKI创建；用`az keyvault secret show --vault-name <vault> --name <secret> --query id -o tsv`确认对象 |
| `trustedClientCaSecrets[].secretVersion` | 本次发布固定的32位Secret版本 | 从上一步返回的Secret ID最后一段取得；禁止写`latest`，轮换必须产生新版本并重新plan |
| `allowedCertificateFqdns` | 客户端叶证书SAN中允许的FQDN | `openssl x509 -in <client-cert.pem> -noout -ext subjectAltName,extendedKeyUsage`核对DNS SAN及Client Authentication EKU；不要填写Admin服务域名来代替设备/主体证书标识 |
| `adminRateLimitPerMinute` | 单公网客户端IP的Admin边缘限流阈值 | 依据管理员人数、企业NAT汇聚及UI实际请求基线确定；先在Detection观察，不照抄示例值作为生产容量承诺 |

客户没有现成的专用边缘信任Vault时，由Vault管理员在**网关同一订阅**创建，不由edge模板隐式新建。下面命令中的值来自客户自己的订阅、资源组、区域和命名规范；先用`az account show`核对租户/订阅。创建者需要资源组写权限，PKI导入人另在该Vault范围获得限时`Key Vault Secrets Officer`，不要给Front Door或运行身份此写角色：

```bash
SUBSCRIPTION_ID="REPLACE_CUSTOMER_SUBSCRIPTION_ID"
TRUST_VAULT_RG="REPLACE_EDGE_TRUST_VAULT_RESOURCE_GROUP"
TRUST_VAULT_NAME="REPLACE_GLOBALLY_UNIQUE_EDGE_TRUST_VAULT_NAME"
LOCATION="REPLACE_AZURE_REGION"

az keyvault create --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$TRUST_VAULT_RG" --name "$TRUST_VAULT_NAME" --location "$LOCATION" \
  --enable-rbac-authorization true --enable-purge-protection true --retention-days 90 \
  --public-network-access Disabled --default-action Deny --bypass AzureServices \
  --query '{id:id,name:name,rbac:properties.enableRbacAuthorization,publicNetworkAccess:properties.publicNetworkAccess,bypass:properties.networkAcls.bypass,purgeProtection:properties.enablePurgeProtection}' \
  --output json
```

如果Vault已存在，不重复运行create；用下列只读命令核对实际状态和同订阅scope。`Disabled + AzureServices`是预期组合，不等于开放任意公网客户端：

```bash
az keyvault show --subscription "$SUBSCRIPTION_ID" \
  --resource-group "$TRUST_VAULT_RG" --name "$TRUST_VAULT_NAME" \
  --query '{id:id,rbac:properties.enableRbacAuthorization,publicNetworkAccess:properties.publicNetworkAccess,defaultAction:properties.networkAcls.defaultAction,bypass:properties.networkAcls.bypass,purgeProtection:properties.enablePurgeProtection}' \
  --output json
```

PKI导入人按客户批准的Preview格式把**不含私钥**的CA链写入Secret后，只记录元数据ID，不把值写入日志：

```bash
az keyvault secret show --subscription "$SUBSCRIPTION_ID" \
  --vault-name "$TRUST_VAULT_NAME" --name "REPLACE_CLIENT_CA_CHAIN_SECRET_NAME" \
  --query '{id:id,enabled:attributes.enabled,contentType:contentType}' --output json
```

输出`id`最后一段就是`secretVersion`。edge配置后由模板只给Front Door profile托管身份授予读权限；证书私钥Vault必须继续保持`bypass=None`，两者禁止复用。

严格模式启用证书吊销检查。客户PKI必须提供Front Door边缘可访问、可用且与证书AIA一致的吊销服务，并实测有效、缺失、错误FQDN、过期和已吊销证书。只验证“有某张CA签发的证书”不足以完成验收。

当前`2026-08-01-preview`公开ARM参考定义了`MtlsCertificateChain`、Key Vault Secret资源ID和固定版本，但没有完整说明Secret值的PEM编码、证书顺序等内容。不得从源站TLS Secret格式自行推断。首次客户发布前须用客户PKI提供的测试CA链在非生产环境完成实际Front Door部署，并确认profile Secret、Admin custom domain均为`Succeeded`；edge-bind已将这些live状态设为强制门禁。服务端仍拒绝时停止并向Microsoft确认Preview约束，不通过关闭验证或改用passthrough绕过。

轮换时可在`trustedClientCaSecrets`同时配置最多两条CA链：先加入新链并部署，发放/验证新客户端证书，再移除旧链并二次部署。Front Door不会自动跟随Vault最新版本；不得覆盖原版本后假定边缘已更新。

## 只读工具

```bash
make validate-stage9

# 默认关闭的预览；不会创建资源
./.venv/bin/python scripts/preview_stage9.py --resource-group <目标资源组> --layer edge
./.venv/bin/python scripts/preview_stage9.py --resource-group <目标资源组> --layer edge-origin

# 本地域名组合预览，仍含待填的身份、镜像和controller占位符
./.venv/bin/python scripts/render_stage7_domain.py --stage 9
kubectl kustomize temp/stage9-domain

# 审批/证据填写于被Git忽略的环境文件，不修改公共模板
./.venv/bin/python scripts/stage9_release.py render --config temp/<环境>/release.json --output-dir temp/<环境>/stage9
./.venv/bin/python scripts/preview_stage9.py --resource-group <目标资源组> --config temp/<环境>/release.json
```

`release.example.json`是失败关闭的结构样例，不是一份批准书。`phase`支持prepare（两个endpoint均关闭）、canary（两个endpoint启用、Detection）、production（Prevention）。开启流量需要实际Front Door ID、两套PLS、固定CA版本及Admin mTLS/吊销/轮换证据。2026-09-10第一阶段改为原生Spend Logs，需验原生记录、权限、留存、容量及故障；只有选增强方案才需L3治理/恢复。协议矩阵与数据库恢复仍为必验，见[当前部署指南](../../docs/customer-deployment-workflows-zh.md)。

`checks`每项包含`passed=true`、`report`、近7天带时区的`observedAt`；changeTicket及两个不同approvedBy对象ID也必须存在。脚本只校验attestation结构/时效，不访问工单验证签名，不代替GitHub Environment审批、Azure RBAC或人工报告评审。

生成物在忽略的`temp/`子目录：`edge.parameters.json`、Stage9 Kustomize overlay、代理policy、admin App Registration回调模板、配置摘要hash。默认域名脚本的输出不带已批准Front Door ID，应用时API和Admin代理都无法启动，不能直接用于生产。

What-if详细结果和diagnostics以0600权限保存在忽略目录，只输出计数。检查拒绝任何Modify/Delete/Unsupported等未知计划；已部署资源的正常后续变更也会被拒绝，需另行审阅，不可自动当成误报跳过。

`stage9_release.py check-origin`可检查ARM GET导出的LB与subnet快照：

```bash
./.venv/bin/python scripts/stage9_release.py check-origin \
  --load-balancer temp/<环境>/lb.json \
  --frontend-id <完整frontend资源ID> \
  --subnet temp/<环境>/subnet.json
```

它不证明快照新鲜度、controller端口规则或网络可达性。需要实际维护者补充来源和拒绝路径测试。

完整范围、回退步骤和已知阻塞见[阶段9记录](../../docs/litellm-stage9-edge-cutover-preparation-2026-09-07.md)。