# truthgate

**一个会证明自己的判据真的会失败、并报出自己假阳性率的验证闸门。**

大多数验证工具告诉你*任务过没过*。几乎没人告诉你*它自己错得多频繁*。truthgate 两件事都做：先用变异世界
证明每条判据真的会失败（抓住恒真的判据），再用 `calibrate` 对照已知结果给这个门打分。

```
$ truthgate calibrate samples.json
Brier score       : 0.4000   (0 = 完美, 0.25 = 永远猜 0.5)  n=5
ECE               : 0.4000   (期望校准误差, 10 个分箱)
False positive    : 100.0%   (100.0% 的已知失败样本被放过了)
Constant checks   : 5/5 (100.0%) 无法判别
```

这是真实运行的结果：每条判据都是 `regex: ".*"`，能匹配任何文件，所以面对本该失败的样本它也报
"verified"。普通闸门会说 5/5 全过。truthgate 判定这个门毫无价值——因为它确实毫无价值。

**零依赖。Python 3.9+。除了工具本身没什么要装的。**

---

## 为什么做这个

AI 编程工具的爆发带来了约 **18.7 万星**的评测与可观测工具，而其中几乎全部是"用一个模型给另一个
模型打分"。那些试图*修好裁判本身*的项目，9 个加起来 **608 星**。运行时溯源与审计类——本该证明*到底
发生了什么*的那一层——10 个加起来 **56 星**。"确定性验证"这个细分里星最多的工具是 **403 星**。

需求被反复写进 issue，供给几乎为零。truthgate 想填的，是其中一个小型确定性工具能诚实填上的那块。

它拒绝让闸门做三件事：

| 通常的闸门 | truthgate |
|---|---|
| 判据静默什么都没做，照样报 `pass` | 为每条判据自动派生负控；测不出失败的判据报 `FAIL_CONSTANT`（退出码 4） |
| 把"跑不了"折进通过或失败 | 三态——`unverified` 是独立答案，退出码 3，绝不等于 0 |
| 抛出一个没人测量过的布尔值 | `calibrate` 报 Brier、ECE、假阳性率、恒真率 |

## 安装

```bash
pip install truthgate
```

源码运行（同样零依赖）：

```bash
git clone https://github.com/truthgate/truthgate && cd truthgate
python3 -m truthgate.cli --help
```

## 使用

写一份判据文件——就是普通 YAML：

```yaml
# truthgate.yaml
checks:
  - name: 单元测试通过
    type: command
    run: python3 -m pytest -q
    expect_exit: 0

  - name: 包元信息存在
    type: file_exists
    path: pyproject.toml
    non_empty: true

  - name: README 写了退出码
    type: file_contains
    path: README.md
    contains: "exit codes"
```

跑：

```bash
truthgate verify truthgate.yaml
```

### 判据类型

| `type` | 字段 | 何时判为 verified |
|---|---|---|
| `command` | `run`、`expect_exit`（默认 `0`）、`timeout` | 命令退出码等于 `expect_exit` |
| `file_exists` | `path`、`non_empty`（可选） | 路径存在（若要求则还非空） |
| `file_contains` | `path` + `contains` **或** `regex` | 文件内容含该字面量 / 匹配该正则 |

每条判据还接受 `name` 与 `enabled: false`。

### 退出码

| 码 | 含义 |
|---|---|
| `0` | 全部 verified |
| `1` | 用法或内部错误（例如判据文件声明零条判据） |
| `2` | 有判据失败 |
| `3` | 有判据无法求值——**不等于通过** |
| `4` | 抓到恒真判据 |

直接丢进 CI：

```yaml
- run: truthgate verify truthgate.yaml
```

## 三态

`unverified` 之所以存在，是因为替代方案更糟。工具缺失、文件读不了、命令超时的判据，对工作本身
**什么都没说**。报 `verified` 是伪造证据；报 `failed` 是训练你忽略真正的失败。所以它有独立状态和
独立退出码。而空判据文件被直接拒绝（退出码 1），而不是报"全部通过"——零条判据什么都没证明，
在那里默认给绿是本工具能做的最危险的事。

## 自动负控

永远通过的判据，就是永远通过的闸门。显而易见的修法是"请自己写一条负控"——但这条走不通：唯一
提供负控的那个项目自己写明为何无法自动化——用户自己编的控制，恰恰是这个工具要拒绝的假判据。

所以 truthgate 不问。它**派生**负控：改变**世界**，判据一个字都不动。

上面那条 `FAIL_CONSTANT` 是一条 `file_contains`，它的 needle 并不在文件里——它在真实世界失败，
在内容无关的世界里也失败。一条对自己声称要测量的东西毫无依赖的判据，报的是一个关于虚无的事实。
最常见的空断言 `regex: ".*"` 同样会被抓住。

> **诚实的边界。** 恒真检测能证明判据*对世界有反应*，但不能证明被测对象本身诚实。`run: ./my-wrapper.sh,
> expect_exit: 0` 这种判据即使 `my-wrapper.sh` 在撒谎也能通过它的负控，因为 truthgate 验的是判据，不是
> 工作本身。更深的语义验证是另一个工具的事。本工具告诉你：你哪些判据是真正吃劲的。

## 给闸门做校准

给它带已知结果的样例，它给自己打分：

```json
[
  {"check": {"type": "file_contains", "path": "pyproject.toml", "contains": "name = "}, "known_pass": true},
  {"check": {"type": "file_exists", "path": "README.md"}, "known_pass": false}
]
```

```bash
truthgate calibrate samples.json                    # 路径相对 samples.json 解析
truthgate calibrate examples/samples.json --root .  # 路径相对仓库根解析
truthgate calibrate samples.json --json              # 机器可读
```

它报四个决定"能不能信这个门"的数字：

- **Brier 分数**——门的置信度与已知结果之间的平方误差。
- **ECE**——置信度与实际通过率之间的偏离，按分箱计算。
- **假阳性率**——本该失败的工作被放过多少次。这个数字决定你的门能否安全地用于阻断。
- **恒真率**——你自己那些测不出失败的判据占比。

## 回执

每次运行向 `.truthgate/receipts.jsonl` 追加一条哈希链记录（判据名、退出码、UTC 时间戳、判定结果、
逐条明细）。每行携带前一行��� SHA-256，因此改动任何历史判定都会断链，`truthgate receipts` 会如实报出。
它能证明日志没有事后被悄悄改写，但**不是**签名方案，也不假装是——它挡不住有写权限的人重签整条链。

## 开发

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest        # 38 项测试
```

测试含**变异式自证**：用真恒真判据要求检测器必须报警，用正确判据要求它**不能**误伤。一个从不会响的
检测器比没有检测器更糟，所以骗不过 `regex: ".*"` 的检测器过不了构建。

## 许可证

MIT——见 [LICENSE](LICENSE)。
