# Stage 9边缘入口

独立资源组级Bicep入口，不自动纳入阶段4/5编排，不修改DNS或部署Kubernetes。默认`deployEdge=false`、`enableApiTraffic=false`、WAF Detection。

`main.bicep`只创建新的Front Door Premium profile/endpoint、`llm-api`自定义域、单个私有origin、精确API route、WAF/security policy和诊断。`llm-admin`只作为输出说明，绝不创建其公共Front Door域/route。无缓存，无默认azurefd.net域路由，无HTTP业务路径。

## 依赖顺序

1. 在新Private AKS建设经过支持周期/安全评审的两个独立私有ingress controller，分别使用`llm-api-ingress`和`llm-admin-ingress` namespace；本模块不安装controller。
2. controller owner创建API专属Standard internal LB frontend，HTTPS证书覆盖`llm-api.<baseDomain>`；配置SSE不缓冲及适当超时。
3. `../edge-origin/main.bicep`引用已审查frontend并创建PLS。NAT subnet需`privateLinkServiceNetworkPolicies=Disabled`，Stage4的`snet-private-ingress`已有此目标设置。
4. 将PLS ID/location输入本模块；Front Door托管Private Endpoint请求必须手工批准并核对profile归属，不能只凭requestMessage。
5. 完成`_dnsauth`域所有权、边缘托管证书以及独立源站证书验证后，再评审启用流量。Microsoft托管边缘证书不安装到ingress。

PLS的`visibility=['*']`允许Front Door跨订阅发现，不代表数据访问授权；`autoApproval=[]`，不得自动批准未知连接。PLS绑定frontend只靠ID不能证明它属于API controller，必须核验LB后端池/rule/selector与controller所有权。不要将admin的frontend或旧公网gateway放入origin。

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

`release.example.json`是失败关闭的结构样例，不是一份批准书。`phase`支持prepare（创建计划、流量关闭）、canary（批准客户端试点、Detection）、production（Prevention）。开启流量需要实际Front Door ID。2026-09-10第一阶段改为原生Spend Logs，需验原生记录/权限/留存/容量及故障；只有选增强方案才需L3治理/恢复。当前发布检查仍使用旧审计字段，须先适配代码及证据，不能跳过或伪造passed。协议矩阵与数据库恢复仍为必验，见[当前部署指南](../../docs/customer-deployment-workflows-zh.md)。

`checks`每项包含`passed=true`、`report`、近7天带时区的`observedAt`；changeTicket及两个不同approvedBy对象ID也必须存在。脚本只校验attestation结构/时效，不访问工单验证签名，不代替GitHub Environment审批、Azure RBAC或人工报告评审。

生成物在忽略的`temp/`子目录：`edge.parameters.json`、Stage9 Kustomize overlay、代理policy、admin App Registration回调模板、配置摘要hash。默认域名脚本的输出不带已批准Front Door ID，应用时将无法启动API代理，不能直接用于生产。

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