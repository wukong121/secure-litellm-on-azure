# Azure Monitor告警转发飞书操作指南

> 核对日期：2026-09-17
>
> 适用：已经执行Stage1 `monitoring`，希望将`ag-litellm-stage1-owner`收到的Azure Monitor告警转发到飞书群。本流程只使用Azure Portal和飞书客户端，不修改仓库模板。

## 1. 方案和边界

调用链：

```text
Azure Monitor告警
  → Action Group ag-litellm-stage1-owner（Common Alert Schema）
  → Logic App Request触发器
  → HTTP POST转换为飞书消息格式
  → 飞书群自定义机器人
```

不能把飞书机器人Webhook直接配置为Action Group的普通Webhook。Azure发送的是Common Alert Schema，飞书要求`msg_type/content`消息格式；中间使用Logic App转换。

本流程保留原有Email通知。Action Group测试只证明通知链路可调用，不证明某条日志查询真的满足阈值，也不等于Stage1验收完成。

## 2. 准备信息和权限

| 项目 | 从哪里取得 | 怎样核验 |
| --- | --- | --- |
| 订阅、旧资源组 | `local_execution/customer.json`中的`azure.subscriptionId`和`legacy.resourceGroup` | 与Portal当前目录/订阅一致 |
| Action Group | Azure Portal → Monitor → Alerts → Action groups | 名称为`ag-litellm-stage1-owner`，Enabled，位于旧资源组 |
| 飞书Webhook | 飞书目标群 → 设置 → 机器人 → 添加机器人 → 自定义机器人 | 形如`https://open.feishu.cn/open-apis/bot/v2/hook/...`，不得写入Git、工单正文或截图 |
| Azure权限 | 客户管理员批准 | 至少可创建/编辑Logic App，并可编辑和测试目标Action Group |

Logic App Consumption按触发和动作次数计费。资源组、区域和数据处理位置须由客户批准；通常使用旧监控资源所在区域或客户专用集成资源组。

## 3. 创建飞书自定义机器人

1. 打开接收告警的飞书群。
2. 进入 **设置 → 机器人 → 添加机器人 → 自定义机器人**。
3. 填写名称，例如`Azure Monitor告警`，完成添加并取得Webhook URL。
4. 在机器人安全设置中至少配置关键词`Azure Monitor`。本文消息正文固定包含该关键词。
5. 不启用签名校验，除非客户另有能够计算飞书HMAC签名的Azure Function或等效转换器；本文的纯Logic App基础路径不生成签名。

Webhook URL等同凭据。泄露后立即在飞书重置机器人Webhook，并更新Logic App。飞书自定义机器人限制单次消息不超过20 KB；本文只发送告警摘要，不转发日志查询结果或正文。

## 4. 创建Logic App

### 4.1 创建资源

1. Azure Portal搜索 **Logic Apps** → **Add**。
2. 选择 **Consumption / Multi-tenant**。
3. 选择客户订阅、获准资源组和区域。
4. 名称例如`logic-litellm-alert-feishu-test`。
5. 完成创建后打开 **Logic app designer**，选择Blank workflow。

### 4.2 添加Request触发器

1. 添加内置触发器 **When an HTTP request is received**。
2. Method选择`POST`。
3. 在 **Request Body JSON Schema** 填入：

```json
{
  "type": "object",
  "properties": {
    "schemaId": {"type": "string"},
    "data": {
      "type": "object",
      "properties": {
        "essentials": {
          "type": "object",
          "properties": {
            "alertRule": {"type": "string"},
            "severity": {"type": "string"},
            "signalType": {"type": "string"},
            "monitorCondition": {"type": "string"},
            "monitoringService": {"type": "string"},
            "targetResourceGroup": {"type": "string"},
            "firedDateTime": {"type": "string"},
            "resolvedDateTime": {"type": "string"},
            "description": {"type": "string"}
          }
        }
      }
    }
  },
  "required": ["schemaId", "data"]
}
```

4. 保存一次，生成HTTP POST URL。不要复制到文档、聊天或公开日志；Action Group稍后直接选择该Logic App。

### 4.3 添加飞书HTTP动作

1. 在触发器下添加内置 **HTTP** Action，重命名为`Send_to_Feishu`。
2. Method：`POST`。
3. URI：填真实飞书机器人Webhook URL。
4. Headers：`Content-Type` = `application/json`。
5. Body切换到文本/代码视图，填入：

```json
{
  "msg_type": "text",
  "content": {
    "text": "@{concat('Azure Monitor告警', decodeUriComponent('%0A'), '规则：', coalesce(triggerBody()?['data']?['essentials']?['alertRule'], 'unknown'), decodeUriComponent('%0A'), '级别：', coalesce(triggerBody()?['data']?['essentials']?['severity'], 'unknown'), decodeUriComponent('%0A'), '状态：', coalesce(triggerBody()?['data']?['essentials']?['monitorCondition'], 'unknown'), decodeUriComponent('%0A'), '信号：', coalesce(triggerBody()?['data']?['essentials']?['signalType'], 'unknown'), decodeUriComponent('%0A'), '资源组：', coalesce(triggerBody()?['data']?['essentials']?['targetResourceGroup'], 'unknown'), decodeUriComponent('%0A'), '时间：', coalesce(triggerBody()?['data']?['essentials']?['firedDateTime'], triggerBody()?['data']?['essentials']?['resolvedDateTime'], 'unknown'), decodeUriComponent('%0A'), '说明：', coalesce(triggerBody()?['data']?['essentials']?['description'], ''))}"
  }
}
```

6. 打开该HTTP Action的 **Settings**，启用 **Secure Inputs** 和 **Secure Outputs**，避免运行历史显示Webhook及响应。

该设置不会把Webhook从Logic App资源定义中加密；能读取工作流定义的管理员仍可能看到URI。限制Logic App读取权限。正式客户若要求集中秘密管理，应改用专用Key Vault和托管身份读取Webhook，不复用证书或业务秘密Vault。

### 4.4 判断飞书是否接受消息

飞书可能在HTTP 200中返回非零业务错误码。仅看HTTP Action绿色不够。同时，本流程采用异步响应：工作流中**不要添加任何Response Action**。Request触发器在没有Response Action时会立即向Action Group返回`202 Accepted`，飞书实际投递结果再由Logic App运行状态判断。

1. 在`Send_to_Feishu`后添加 **Condition**。
2. Condition顶部如果只显示`AND`或`OR`，这是多条条件的组合方式，不是表达式输入框。本流程只有一条条件，选择`AND`。
3. 在`AND`组内点击 **+** 或 **New item → Add row**，新增一条比较行。不要把下面的表达式填到`AND / OR`选择框中。
4. 点击新行左侧的 **Choose a value**，选择 **Expression / fx**，填入：

```text
int(coalesce(body('Send_to_Feishu')?['code'], body('Send_to_Feishu')?['StatusCode'], -1))
```

5. 中间运算符选择 **is equal to**；右侧 **Choose a value** 选择 **Expression / fx** 并填数字`0`。
6. 如果使用JSON代码视图，条件应包含下面的表达式；代码视图中的函数表达式必须以`@`开头：

```json
"expression": {
  "and": [
    {
      "equals": [
        "@int(coalesce(body('Send_to_Feishu')?['code'], body('Send_to_Feishu')?['StatusCode'], -1))",
        0
      ]
    }
  ]
}
```

7. True分支可以留空；为了在运行历史中清楚显示，可添加 **Compose**，重命名为`Feishu_delivered`，Inputs填`delivered`。不要添加Response。
8. False分支添加 **Terminate**：Status=`Failed`，Code=`FeishuRejected`，Message=`Feishu webhook returned a nonzero code`。
9. 删除工作流中已有的所有 **Response** Action并保存。Response只放在某个条件分支时，如果该分支被跳过，Action Group会收到HTTP 502，即使`Send_to_Feishu`已经把消息发出。

## 5. 绑定Action Group

1. Azure Portal → **Monitor → Alerts → Action groups**。
2. 打开`ag-litellm-stage1-owner`，选择 **Edit**。
3. 保留现有Email notification，不删除。
4. 进入 **Actions** → **Add action**。
5. Action type选择 **Logic App**，Action name例如`feishu-alert`。
6. 选择第4节创建的Logic App和Request工作流。
7. 将 **Common alert schema** 设为`Yes`。这是每个Action独立的设置，Email已启用不代表Logic App自动启用。
8. 保存Action Group。

此修改是Portal手工配置。重新执行本仓库的`--step monitoring`会按Bicep定义更新同一个Action Group，可能移除手工添加的Logic App Action。每次重跑monitoring后都必须重新核对、必要时重新绑定并测试；长期生产使用应把集成纳入客户自己的IaC。

## 6. 测试与通过标准

1. 在Action Group页面点击 **Test**。
2. 选择与当前规则匹配的样本类型，例如 **Log Alert V2**。
3. 只勾选或明确包含`feishu-alert`动作，开始测试并等待结果。
4. 打开Logic App → **Runs history**，确认本次运行Succeeded、`Send_to_Feishu`成功且Condition走True分支。
5. 在飞书群确认收到一条以`Azure Monitor告警`开头的消息。
6. 核对规则名、级别、状态和时间不是空值或明显错误。

通过标准必须同时满足：

- Action Group测试显示Success，表示Logic App请求已被`202 Accepted`接收。
- Logic App运行成功，`Send_to_Feishu`成功且Condition走True分支。
- 飞书Webhook返回`code=0`。
- 目标飞书群实际收到消息。

Action Group测试使用样本负载，不会让真实告警计数增加，也不能证明日志规则已经被真实条件触发。若Stage1验收要求端到端规则评估，必须另行批准无业务影响的测试规则或测试事件。

## 7. 常见问题

| 现象 | 检查 |
| --- | --- |
| Action Group找不到Logic App | Logic App必须已保存，并使用Request HTTP触发器；确认订阅和权限 |
| 消息已收到，但Action Group显示Logic App HTTP 502 | 工作流中存在只位于部分分支的Response，本次没有执行到它；删除所有Response并保存，让Request触发器异步返回202 |
| 消息已收到，但Condition走False | JSON代码视图中的表达式可能缺少开头的`@`，或飞书返回的是`StatusCode`/字符串`"0"`；使用第4.4节的`int(coalesce(...))`表达式 |
| Logic App触发但飞书没有消息 | 查看Runs history中的Condition；确认飞书返回`code=0`、机器人仍在目标群 |
| 飞书返回`19024` | 关键词校验失败；正文必须包含配置的`Azure Monitor`关键词 |
| 飞书返回`19022` | IP白名单不包含Logic App出口；核对Logic App Properties中的Outbound IP addresses，或改用客户批准的安全方式 |
| 飞书返回`19021` | 已启用签名但当前流程未生成签名；接入签名计算器或按批准策略调整机器人安全设置 |
| Action Group测试成功但Logic App失败 | Action Group调用入口成功不代表下游投递成功；以Logic App运行和飞书`code=0`为准 |
| 重跑monitoring后飞书Action消失 | Bicep覆盖了Portal手工修改；重新绑定并测试，或将其纳入客户IaC |

## 8. 停用和轮换

- 临时停用通知：在Action Group中禁用或删除`feishu-alert`动作，不删除告警规则。
- Webhook泄露：在飞书重置Webhook，更新Logic App URI并重新测试。
- 删除机器人前先从Action Group移除Logic App Action，避免持续失败和重试。
- 保留变更单、Action Group测试时间、Logic App Run ID和飞书实际收件确认；不保存Webhook URL。

参考：

- [Azure Monitor Action Groups](https://learn.microsoft.com/azure/azure-monitor/alerts/action-groups)
- [Azure Monitor Common Alert Schema](https://learn.microsoft.com/azure/azure-monitor/alerts/alerts-common-schema)
- [Azure Logic Apps Request trigger](https://learn.microsoft.com/azure/connectors/connectors-native-reqres)
- [飞书自定义机器人使用指南](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)