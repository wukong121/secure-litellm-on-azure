# LiteLLM Kustomize 应用层

## Stage 9边缘入口准备

`components/stage9-edge`在API ingress中只保留6个推理Exact路径及私有`/readyz`，API代理增加`FRONT_DOOR_ID`补充检查；admin保持独立私有ingress。`validation/stage9`组合前序组件，未加入环境overlay。运行`make validate-stage9`和域名生成器`--stage 9`验证；占位ID、镜像与IngressClass必须经批准替换。

Front Door/PLS模板位于`infra/edge`和`infra/edge-origin`；默认关闭且不创建DNS。此组件不会安装controller、创建内部LB或解禁阶段7关闭协议。发布/回退和未完成Gate见[阶段9记录](../docs/litellm-stage9-edge-cutover-preparation-2026-09-07.md)。

## Stage 8审计组件

`components/stage8-audit`与`validation/stage8`包含L3/OTLP/输入Content Safety配置、审批/保全只读挂载、暂停的独立清理CronJob、Collector和网络策略。所有功能默认关闭，未加入环境overlay。执行`make validate-stage8`回归；域名生成可用`scripts/render_stage7_domain.py --stage 8`。

先通过`infra/audit-storage`建设独立私有CMK存储并验证各身份权限，再通过批准配置开启采集及清理。`/audit`是独立审计角色查看页，不是原有Admin UI解禁。配置/接口/首期限制见[阶段8记录](../docs/litellm-stage8-l3-audit-observability-2026-09-07.md)。

本目录是应用交付骨架，不会自动部署到当前集群。

## 安全基线

Base清单包含：

- 两副本和滚动更新；
- ClusterIP Service，不创建公网LoadBalancer、NodePort或Ingress；
- 专用ServiceAccount且默认不自动挂载Kubernetes API Token；
- UID/GID `10001`、非Root、RuntimeDefault seccomp；
- 禁止提权、drop ALL capabilities、只读根文件系统；
- `/tmp`受限emptyDir；
- startup/readiness/liveness probes；
- requests/limits；
- PDB和topology spread；
- Spend Logs保留7天且Prompt/Response正文关闭；
- Secret逐项引用，不使用`envFrom`。

## 环境覆盖

- `dev`：1副本，仅用于开发验证；
- `test`：2副本；
- `prod`：2副本。

当前镜像固定为阶段2验证过的官方LiteLLM `1.98.0` digest。该清单只用于渲染和后续新环境验证；在镜像同步、扫描、SBOM和签名完成后，供应链流程必须通过Pull Request把镜像替换为目标ACR digest。

## 尚未投入环境Overlay

以下内容尚未进入dev/test/prod环境Overlay，不能以占位符假装已经部署：

- Workload Identity ServiceAccount注解和Pod标签已有Stage4组件；
- Key Vault CSI `SecretProviderClass`和逐项Secret同步已有Stage5组件；
- NetworkPolicy已有Stage4组件；
- Internal Load Balancer/Private Link Service；
- Front Door/WAF；
- PostgreSQL Flexible Server和Managed Redis目标连接已有Stage5组件，但未执行迁移及协议验收；
- HPA/KEDA指标；
- 生产模型列表、Router目标配置、客户自有Entra认证代理和Guardrail。

## 验证

从仓库根目录运行：

```bash
kubectl kustomize deploy/overlays/dev
kubectl kustomize deploy/overlays/test
kubectl kustomize deploy/overlays/prod
```

不要直接将阶段3清单应用到当前生产集群。目标新环境完成Private AKS、Workload Identity、Key Vault、PG和Redis后，才能生成可部署overlay。

## Stage 4 NetworkPolicy组件

`deploy/components/stage4-network`提供：

- namespace Default Deny；
- 仅允许ingress-nginx到LiteLLM 4000；
- 显式DNS出口；
- 到Private Endpoint subnet的443/5432/6380/10000；
- 受Firewall进一步约束的公网HTTPS出口；
- 明确排除IMDS地址，防止继续依赖节点身份；
- Workload Identity ServiceAccount和Pod标签patch。

ServiceAccount Client ID保持占位符，必须由受保护的Bicep部署输出注入，禁止把真实Client ID提交到仓库。

该组件尚未加入prod overlay，因为Key Vault、PG、Redis和实际DNS/IP依赖仍未建设。未完成协议和故障测试前不得应用到生产。

## Stage 5数据组件

`deploy/components/stage5-data`提供：

- Key Vault Secrets Store CSI `SecretProviderClass`；
- Master Key、永久Salt和`DATABASE_URL`逐项同步到`litellm-runtime-secrets`；
- LiteLLM Pod只读CSI挂载；
- Azure Managed Redis主机、端口和Workload Identity Object ID注入；
- `azure_redis_ad_token=true`、TLS、证书链和主机名校验。

由于LiteLLM当前以环境变量读取上述三个Secret，CSI同步仍会在Kubernetes中产生Secret对象。这是明确记录的过渡补偿方案，不是“Secret不落etcd”。启用前必须确认etcd静态加密、Secret最小RBAC、namespace隔离、轮换行为和未授权读取失败。

`deploy/validation/stage5`仅用于同时渲染Stage4和Stage5组件。其中所有`REPLACE_*`值必须由受保护部署输出替换；不得直接应用带占位符的清单。Redis端口固定为10000，并使用Workload Identity Object ID作为Entra用户名。PostgreSQL连接串必须包含`sslmode=verify-full`并作为单个Key Vault Secret引导。

## Stage 6高可用与路由组件

`deploy/components/stage6-ha`提供：

- 固定双副本、零不可用滚动更新和30秒稳定Ready窗口；
- 600秒终止窗口及30秒Endpoint摘除等待；
- CPU/内存HPA，2至6副本，600秒缩容稳定窗口；
- Git管理的模型组、稳定`model_info.id`、Router和日志安全策略模板；
- `simple-shuffle`、四类affinity及Redis Entra/TLS共享状态；
- 明确禁止未经A/B批准的`usage-based-routing-v2`。

`deploy/validation/stage6`组合Base和Stage4/5/6组件，仅用于渲染与静态验证。真实模型名称、端点和稳定ID仍是`REPLACE_*`，不能直接应用。`preStop sleep 30`只为Endpoint传播预留时间，不是LiteLLM drain证明；WebSocket/SSE、节点drain、HPA和故障行为必须在新环境实测。

运行`make validate-stage6`执行全部前序回归和Stage6门禁。

## Stage 7双子域与认证代理

`components/stage7-identity`与`validation/stage7`保留`llm-api.example.com`、`llm-admin.example.com`通用模板。主域通过环境参数`baseDomain`配置：当前验证域存于忽略的`auth-proxy/domain.local.json`，客户独立提供自己的值。运行`./.venv/bin/python scripts/render_stage7_domain.py`生成忽略的`temp/stage7-domain`预览overlay；它同步更新代理Host、Ingress/TLS与OIDC回调，不部署资源，其他占位符仍需补齐。两个入口对应独立Deployment、ServiceAccount、CSI挂载和NetworkPolicy，后端只允许代理访问，不再允许ingress直达。

源码和配置契约见`auth-proxy/README_ZH.md`；Node 24，首次执行`npm ci --prefix auth-proxy --ignore-scripts`，再运行`make validate-stage7`。尚未加入任何环境overlay。

管理面是OIDC保护的有限管理API，不是已经完成无感登录的原生Admin UI。WebSocket、Files、MCP、原生UI与Responses对象引用/加密多轮上下文默认拒绝，必须补齐授权验收才可放行。两个独立Vault/UAMI、私有ingress controller、DNS、证书、真实Entra角色和MFA均未部署。
