# Codex `image_gen` Namespace 冲突回归报告

> **2026-08-10旧版本/隔离回归证据。** 下文`1.95.0`、loopback和图像路径结果不描述当前West US 3 `1.104.0`源码构建或Front Door private native网关；当前用户Codex Responses推理成功也不证明图像路径或全部工具namespace矩阵。当前版本的有界证据见[升级验证](../docs/litellm-1.104.0-upgrade-validation-2026-10-07.md)，发布/协议验收见[本地Stage2–9指南](../local_execution/stage2-9-guide-zh.md)。

## 测试目的

验证升级到官方 LiteLLM `1.95.0` 后，Codex 通过 Responses API 调用普通 GPT 模型时，是否仍会出现以下错误：

```text
litellm.BadRequestError: AzureException BadRequestError - {
  "error": {
    "message": "Invalid Value: 'tools.namespace'. User-defined namespace 'image_gen' collides with an existing tool namespace.",
    "type": "invalid_request_error",
    "param": "tools.namespace",
    "code": null
  }
}. Received Model Group=gpt-5.6-sol
Available Model Group Fallbacks=None
```

该问题发生在普通 GPT 模型的 Responses 工具定义中，不要求 Azure OpenAI Resource 部署 `gpt-image-2`、DALL-E 或其他图像生成模型。

## 测试环境

测试日期：2026-08-10

| 项目 | 值 |
|---|---|
| LiteLLM 镜像 | `docker.litellm.ai/berriai/litellm:1.95.0` |
| AKS Deployment | `litellm/litellm-mi-proxy` |
| Deployment 状态 | `1/1 Ready` |
| Responses 模型组 | `gpt-5.6-sol` |
| 上游认证 | Azure Managed Identity |
| 客户端认证 | LiteLLM Bearer Key |
| API | `POST /v1/responses` |

## 回归载荷

客户报错的核心是 Codex 发送的用户自定义 namespace：

```json
{
  "type": "namespace",
  "name": "image_gen",
  "description": "Tools in the image_gen namespace.",
  "tools": [
    {
      "type": "function",
      "name": "imagegen",
      "description": "Generate an image.",
      "strict": false,
      "parameters": {
        "type": "object",
        "properties": {
          "prompt": {
            "type": "string"
          }
        },
        "required": ["prompt"],
        "additionalProperties": false
      }
    }
  ]
}
```

测试同时包含两个控制组，用于区分 namespace 冲突与 Azure 内置图像生成工具的配置要求：

1. 只发送 `namespace=image_gen`。
2. 只发送内置 `type=image_generation`。
3. 同时发送内置 `image_generation` 和 `namespace=image_gen`。

## 可复现命令

以下命令在 LiteLLM Pod 内调用本机代理，不读取或输出明文 Key：

```powershell
@'
import os
import httpx

headers = {
    "Authorization": f"Bearer {os.environ['LITELLM_MASTER_KEY']}",
    "Content-Type": "application/json",
}
namespace_tool = {
    "type": "namespace",
    "name": "image_gen",
    "description": "Tools in the image_gen namespace.",
    "tools": [{
        "type": "function",
        "name": "imagegen",
        "description": "Generate an image.",
        "strict": False,
        "parameters": {
            "type": "object",
            "properties": {"prompt": {"type": "string"}},
            "required": ["prompt"],
            "additionalProperties": False,
        },
    }],
}

cases = [
    ("namespace_only", [namespace_tool]),
    ("builtin_only", [{"type": "image_generation"}]),
    ("both", [{"type": "image_generation"}, namespace_tool]),
]

with httpx.Client(timeout=120) as client:
    for case_name, tools in cases:
        response = client.post(
            "http://127.0.0.1:4000/v1/responses",
            headers=headers,
            json={
                "model": "gpt-5.6-sol",
                "input": "Reply only: ok. Do not call tools.",
                "store": False,
                "max_output_tokens": 32,
                "tools": tools,
            },
        )
        print(f"{case_name}: HTTP {response.status_code}")
        if not response.is_success:
            print(response.text[:1200])
'@ | kubectl exec -i -n litellm deploy/litellm-mi-proxy -- python -
```

## 测试结果

| 测试用例 | HTTP 状态 | 结果 |
|---|---:|---|
| `namespace_only` | `200` | 成功返回 Responses message，未出现 namespace collision |
| `builtin_only` | `400` | 要求配置 `x-ms-oai-image-generation-deployment` |
| `both` | `400` | 要求配置 `x-ms-oai-image-generation-deployment`，未出现 namespace collision |

`namespace_only` 的实际结果：

```text
CASE=namespace_only
STATUS=200
RESPONSE_ID_PRESENT=True
OUTPUT_TYPES=message
```

两个控制组返回：

```text
imagegen deployment must be provided through header:
x-ms-oai-image-generation-deployment
```

这不是 `tools.namespace` 冲突，而是显式启用 Azure 内置 `image_generation` 工具后缺少图像模型 Deployment 配置。

## 真实 Codex 客户端验证

除构造 HTTP 载荷外，还使用本机 Codex CLI 通过 LiteLLM Provider 调用 `gpt-5.6-sol`：

```powershell
$encoded = kubectl get secret litellm-env -n litellm `
  -o jsonpath='{.data.LITELLM_MASTER_KEY}'
$env:LITELLM_API_KEY = [Text.Encoding]::UTF8.GetString(
  [Convert]::FromBase64String($encoded)
)

try {
  codex exec `
    -c 'model_provider="litellm"' `
    -m gpt-5.6-sol `
    --sandbox read-only `
    --json `
    'Reply with exactly: NAMESPACE-REGRESSION-OK'
} finally {
  Remove-Item Env:LITELLM_API_KEY -ErrorAction SilentlyContinue
}
```

实际结果：

```text
EXIT_CODE=0
NAMESPACE-REGRESSION-OK
turn.completed
```

Codex 同时输出以下非阻塞弃用提示：

```text
`[features].imagegenext` is deprecated. Use `[features].image_generation` instead.
```

当前不要仅为消除该提示而启用 `[features].image_generation`。后者会发送 Azure 内置 `image_generation` 工具；如果客户没有图像模型 Deployment，将得到缺少 `x-ms-oai-image-generation-deployment` 的 `400` 错误。

## 结论

在以下组合中，客户原始的 `image_gen` namespace collision 已无法复现：

```text
LiteLLM 1.95.0
+ Azure Responses API
+ gpt-5.6-sol
+ Codex image_gen namespace
```

namespace-only 构造请求和真实 Codex CLI 请求均成功，因此可以判定当前部署已解决客户遇到的 namespace 冲突。

该结论不代表以下两个独立功能已经验证通过：

- Azure 内置 `image_generation` 工具：需要单独配置图像模型 Deployment 和 `x-ms-oai-image-generation-deployment`。
- LiteLLM `/images/generations` 使用 Managed Identity：官方 `1.95.0` 的独立回归仍返回 `401`，与本报告的 Responses namespace 问题无关。

## 回归判定标准

后续升级 LiteLLM、Codex 或 Azure API version 时，应重新执行本报告的三组请求和真实 Codex 测试：

- 通过：`namespace_only` 返回 `200`，真实 Codex 返回成功消息。
- 失败：响应包含 `param=tools.namespace` 或 `collides with an existing tool namespace`。
- 非本问题：响应仅要求 `x-ms-oai-image-generation-deployment`。