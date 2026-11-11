# 验收用例

一条验收用例写在 `acceptance.feature`，执行证据写在 `verify.md`，对抗会话用 `GDD-ADVERSARIAL` 指回这条用例。

Gherkin 使用 `Feature`、`Scenario`、`Given`、`When`、`Then`。本文件只规定本仓库怎么把一条用例和它的证据、裁决对上，不另写 Gherkin 语法。

DoD 的分层验证结果写在 `test-report.md` 的 `## 验证结果`。那份报告不代替这里的用例和证据。

绑定迭代后，两份文件在 `iterations/<名字>/` 下。未绑定迭代时，沿用模板里写明的路径。

## 1. acceptance.feature

文件至少有一行 `Feature:`。一条用例是一行 `Scenario:`。用例 id 是 `Scenario:` 后面去掉首尾空白的标题。同一文件里标题唯一。

```gherkin
Feature: 验收
  Scenario: 主路径
    Given 就绪
    When 执行
    Then 通过
```

上例的用例 id 是 `主路径`。

内置交付模板的出口检查只要求文件里出现 `Feature:`。harness 不解析 Gherkin；标题是否唯一、步骤是否完整，由写作者和对抗会话对照本契约。

## 2. verify.md

每个用例一节，标题与 `Scenario` 的 id 相同。节内写明：

- 使用的数据
- 过程日志
- 落盘数据

```markdown
## 主路径

- 使用的数据：账号 demo，订单 1001
- 过程日志：启动服务后调用下单接口，返回 200
- 落盘数据：订单表新增一行，状态为已创建
```

内置交付模板的出口检查只要求 `verify.md` 里出现「使用的数据」。三个字段是否逐条齐全，由写作者和对抗会话对照本契约。

## 3. 对抗裁决

对抗会话独立于测试主体。它阅读 `acceptance.feature` 和 `verify.md`，不重跑测试，也不复用测试主体的结论。

对照 Gherkin 判定每个用例。证据支撑不了结论，或环境缺口被记成通过，都判 fail。在回复最末一行单独写出。说明里的示例不要原样抄进结果。

`GDD-ADVERSARIAL:` 后接一行 JSON，对象里有 `cases` 数组。harness 取最后一条 `cases` 为数组的合法行。下面这一行把 `cases` 写成了字符串，不会被当成裁决：

```text
GDD-ADVERSARIAL: {"cases":"每条用例的 id、result、cause、reason"}
```

`cases` 里每一项：

| 字段 | 取值 |
|---|---|
| `id` | `acceptance.feature` 里的 `Scenario` 标题 |
| `result` | `pass` 或 `fail`。其它值按未通过 |
| `cause` | `code`、`test`、`spec`、`env` 之一 |
| `reason` | 这一条为什么通过或失败 |

`cause` 的含义：

| 值 | 含义 |
|---|---|
| `code` | 实现没有满足用例 |
| `test` | 用例执行、数据或记录本身有问题 |
| `spec` | `goal.md`、`spec.md`、`plan.md` 或已确认的决定写错了 |
| `env` | 环境或依赖导致跑不起来。能自己解决的应该已经解决（例如 mock），不能被记成通过 |

`cause` 为 `env` 且 `result` 为 `pass` 时，harness 改记为 `fail`。`cases` 为空不能当成全部通过。

失败的去向：`spec` 进入决策定级；`code` 退回实现，并带上同一轮全部失败用例；其余失败留在测试。定级规则见 `docs/covenant/decision-grade.md`。
