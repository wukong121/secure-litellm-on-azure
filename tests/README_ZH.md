# LiteLLM on Azure验证

[English](README.md) | [项目总览](../README_ZH.md) | [客户迁移指南](../docs/customer-migration-guide-zh.md)

测试覆盖客户配置、迁移门禁、Azure/Kubernetes模板契约、认证/审计代理和LiteLLM运行时行为。离线验证、隔离运行时验证和客户真实环境验收必须分开。

## 离线验证

在仓库根目录运行，需已安装[Python依赖](../requirements.txt)、Node.js 24及auth-proxy依赖、Azure CLI/Bicep和kubectl：

```bash
make validate-stage9
.venv/bin/python -m unittest tests.test_customer_migration tests.test_customer_templates tests.test_public_config
```

- Stage9包含全部前序门禁：Bicep编译、Kustomize渲染、安全策略、Node测试及合成L3闭环，不部署资源。
- 客户测试验证配置/证据绑定、目标隔离、workflow约束及生成参数与Bicep契约的一致性。
- 发布检查拒绝非示例身份和明显凭据，不读取忽略的客户文件。
- 旧部署单元测试使用模拟Azure/Kubernetes调用，覆盖订阅、配置兼容、探针、PVC和Salt保护。

## 隔离OSS回调验证

```bash
make validate-oss-callbacks
```

需要Docker和固定版本LiteLLM镜像；容器无外部网络，使用合成loopback上游验证SDK及真实Proxy，不向宿主环境安装LiteLLM。未缓存镜像可能需要下载。范围和限制见[回调实测记录](../docs/litellm-oss-callback-validation-2026-09-07.md)。

## 旧网关真实环境测试

[test_all_deployments.py](test_all_deployments.py)是仅用标准库的CLI，会向明确指定的旧网关发送真实模型请求并可能产生费用。它不是Entra登录客户端，也不是新安全入口的验收套件。需要批准的HTTPS入口、客户模型配置和受限Virtual Key。

从客户秘密管理系统向进程注入`API_KEY`后，在仓库根目录运行：

```bash
python tests/test_all_deployments.py \
  --config LiteLLM/azure-openai.loc.json \
  --base-url "https://<approved-legacy-gateway-host>" \
  --prompt "synthetic validation"
```

不要把Master Key放进命令行，不使用客户真实Prompt，不上传原始响应到公开日志。脚本为旧路由同时发送Bearer和`api-key`头。Chat和图像分别测试OpenAI/Azure风格路径；Sora仅检查模型注册，不证明视频生成功能正常。

新认证代理只允许有限的Entra授权路径。旧网关的Azure风格路径、图像/视频、WebSocket或加密多轮测试结果不能证明新入口兼容，更不能作为绕过策略的理由；真实租户、客户必需Codex协议、私网/身份、数据库迁移、L3可靠恢复和回退须通过对应[阶段门禁](../docs/customer-migration-guide-zh.md)。

## Codex 缓存命中与会话亲和测试

以下操作会调用真实Codex和模型服务；应在批准的旧基线或隔离环境中执行，模型名称使用客户实际别名，结果保留在Git之外。示例数值不是客户性能承诺。

### 前置条件

- Codex `config.toml` 中已经配置 `model_providers.litellm`；
- Provider 使用 `wire_api = "responses"`；
- LiteLLM 已配置 `responses_api_deployment_check`、`deployment_affinity` 和 `session_affinity`；
- 测试模型已经由 LiteLLM 暴露；
- 本机已安装 Codex CLI；
- 使用 `--verify-backend-affinity` 时，当前 `kubectl` context 必须能访问 LiteLLM AKS。

先在当前 PowerShell 会话设置 LiteLLM Virtual Key：

```powershell
$env:LITELLM_API_KEY = Read-Host "LiteLLM Virtual Key"
```

### 为什么不能只测两轮

在连续任务中，第 $n$ 轮请求包含此前历史和本轮新增输入。即使可复用前缀全部命中，两轮时 `cached_input_tokens / input_tokens` 也通常只有约 50%；随着轮数增加，最终轮才会逐步接近 90%。因此脚本默认模拟 10 轮完整工程任务，依次覆盖需求、风险、架构、安全、可靠性、成本、测试、发布和最终评审。

### 第一次：记录 simple-shuffle 基线

先临时关闭 `optional_pre_call_checks` 并保持 `routing_strategy: simple-shuffle`。`--routing-mode` 只标记结果和选择验收规则，**不会自动修改生产 Router 配置**。

配置生效并完成 Pod rollout 后运行：

```powershell
python .\tests\test_codex_cache_affinity.py `
  --model gpt-5.6-terra `
  --sizes 1024,4096,8192 `
  --rounds 10 `
  --routing-mode simple-shuffle `
  --verify-backend-affinity `
  --output-json "$env:TEMP\codex-cache-simple-shuffle.json"
```

simple-shuffle 模式只采集基线，不会因为缓存率低或后端发生切换而返回失败。

### 第二次：启用 affinity 并比较

重新启用：

```yaml
optional_pre_call_checks:
  - responses_api_deployment_check
  - deployment_affinity
  - session_affinity
deployment_affinity_ttl_seconds: 3600
```

完成 rollout 后运行：

```powershell
python .\tests\test_codex_cache_affinity.py `
  --model gpt-5.6-terra `
  --sizes 1024,4096,8192 `
  --rounds 10 `
  --routing-mode affinity `
  --min-final-hit-rate 0.85 `
  --min-prefix-continuity 0.90 `
  --verify-backend-affinity `
  --compare-json "$env:TEMP\codex-cache-simple-shuffle.json" `
  --output-json "$env:TEMP\codex-cache-affinity.json"
```

每个 `--sizes` 档位都会执行：

1. 创建全新的 Codex Session；
2. 首轮注入该规模的稳定项目背景；
3. 使用同一个 Session 连续完成 10 个不同的工程子任务；
4. 从每轮 `turn.completed` 读取真实 `input_tokens` 和 `cached_input_tokens`；
5. 输出逐轮缓存曲线、最终轮、稳态、全任务加权和前缀连续率；
6. 查询 LiteLLM Spend Logs，以稳定 `cache_key` 关联全部轮次；
7. 统计唯一 `model_id` 数和相邻轮次的后端切换次数；
8. 与 simple-shuffle JSON 按相同 Payload 档位计算指标差值。

`--sizes` 是用于构造 Payload 的近似 Token 数。最终统计始终使用 Codex 返回的实际 Token 数，因为 Codex 的系统指令、工具定义和会话历史也会计入输入。

affinity 实测的逐轮曲线示例：

```text
round  input_tokens  cached_tokens  total_rate  previous_prefix
1      32444         16079          49.56%      WARM
2      49436         32438          65.62%      99.98%
3      66623         49427          74.19%      99.98%
...
10     192880        174109         90.27%      99.98%

payload  rounds  final_rate  steady_rate  aggregate  continuity  backends  transitions  status
512      10      90.27%      89.30%       85.15%     100.00%     1         0            PASS
```

字段解释：

| 字段 | 含义 |
|---|---|
| `payload` | 请求构造的近似 Payload Token 档位 |
| `final_rate` | 最后一轮 `cached_input_tokens / input_tokens`；10 轮 affinity 预期接近 90% |
| `steady_rate` | 最后三轮命中率的算术平均 |
| `aggregate` | 除首轮外，所有轮次缓存 Token / 所有轮次输入 Token |
| `continuity` | `cached_input_tokens` 至少覆盖上一轮输入 90% 的轮次比例 |
| `backends` | 同一 `cache_key` 在 Spend Logs 中出现的唯一 `model_id` 数 |
| `transitions` | 按时间排列后，相邻轮次 `model_id` 变化的次数 |
| `status` | affinity 模式下最终轮、前缀连续率和单后端校验是否通过 |

LiteLLM `1.95.0` 的 WebSocket Spend Logs 可能显示 `prompt_tokens=0`、`api_base` 为空，因此脚本不会使用这两个字段关联请求。Codex 会为同一会话发送稳定 `prompt_cache_key`，LiteLLM 将其记录为相同 `cache_key`；脚本以该字段关联完整任务的全部轮次，并用 `model_id` 序列判断后端亲和。

测试完成后清理环境变量：

```powershell
Remove-Item Env:LITELLM_API_KEY -ErrorAction SilentlyContinue
```