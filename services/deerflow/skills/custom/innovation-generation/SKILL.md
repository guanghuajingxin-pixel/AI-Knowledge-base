---
name: innovation-generation
name_en: innovation-generation
name_zh: 创新生成
description: Conversational innovation-proposal authoring for the Jack Spark (midflow) platform, plus legacy daily scan. One-sentence idea → the skill auto-fills every submission field, renders the full proposal as a DingTalk interactive card (buttons / checkboxes / input boxes / forms) for confirmation, saves a draft on tap, then a second card button starts the approval flow. Stateful stage machine + in-conversation working memory: after every user tap it continues from the exact stage instead of restarting the skill. Use when the user mentions 提创新/创新提案/创新提报/金点子/星火/创新评审流程/每日创新摘要, or replies to one of this skill's cards.
description_en: "Conversational innovation-proposal authoring for the Jack Spark (midflow) platform, plus legacy daily scan. One-sentence idea → auto-fills fields, confirms via DingTalk interactive cards, saves a draft, then starts the approval flow on demand. Stateful: continues from the stage where the user last interacted instead of restarting."
description_zh: "面向杰克星火（midflow）流程云平台的对话式创新提案提报。用户一句话口述，技能自动补齐创新提报全字段，以钉钉互动卡片（按钮/复选/输入框/表单）回传确认，确认后写入草稿，再以卡片按钮正式发起创新流程。采用阶段机+工作记忆，用户每次交互后从当前阶段直接续跑，不再重头执行。"
argument-hint: Describe the innovation idea in one sentence, or paste a meeting note / document link; you can also tap a card button I sent
argument-hint-en: Describe the innovation idea in one sentence, or paste a meeting note / document link; you can also tap a card button I sent
argument-hint-zh: 一句话说明创新想法，或贴会议纪要/文档链接；也可以直接点击我发的卡片按钮
user-invocable: true
version: 3.1.0
install_method: upload
---

# 创新提案助手

## 0 运行模型：单轮续跑（先读本节，优先级最高）

本技能强制使用「工作记忆 + 阶段机 + 入口判定」：任何一轮都先判定"从哪继续"，交互结果到达后**直接执行对应动作**，禁止从头再来。

### 0.1 工作记忆 `inn_state`（会话内持续维护）

把全流程状态保存在一段会话内 JSON 中，每完成一个动作就更新一次，交互后直接读取续跑：

```json
{
  "stage": "A4_WAIT",
  "mode": "conversational",
  "idea": "一句话想法原文",
  "fields": {
    "tm": "", "gslb": "", "sfygs": "", "gshcxzjtr": "", "gshsy": "",
    "gsqgsms": "", "gshgsms": "", "gshsyms": "", "ctar": ""
  },
  "source": {},
  "evidence": [],
  "tk": "",
  "initData": {},
  "bizId": "",
  "draftOk": false,
  "ledgerRowId": ""
}
```

- `source`：每个字段的来源标注（`[用户口述]` / `[自动补齐]` / `[自动判定]` / `[需你填]` / `[占位]` / `[默认]`）。
- `evidence`：钉钉检索到的证据，每条含来源+日期。
- `tk`：本会话 token，**取一次复用，跨会话不复用**。
- `initData`：initializeData 结果，**取一次复用**。
- `bizId`：草稿 ID，A5 成功后写入。

### 0.2 阶段机

```
A1 收集 → A2 补齐 → A3 生成 → A4_WAIT(确认草稿) ─确认→ A5 写草稿 → A6_WAIT(确认发起) ─直接发起→ A7 发起 → A8 留痕 → DONE
                ▲                 │还要修改/再改改                     │留草稿 ────────────────┘
                └─────────────────┘                                   │再改改 → A3
```

- 等待态只有两个：`A4_WAIT`（提交草稿前）与 `A6_WAIT`（发起流程前）。
- 每次用户交互后，**先把 `stage` 更新为实际去向，再执行对应动作**。

### 0.3 每轮入口判定（解决"交互后重跑 SKILL.md"）

收到任何用户消息，第一步查 `inn_state.stage`：

1. **存在等待态且本条消息是交互结果**（卡片按钮 value、卡片表单提交、或文字答复）→ 查 0.5 映射，**从当前阶段直接执行**，不做任何重复工作。
2. **无工作记忆**，或用户消息明确开启新提案（"再提一个"、"重新报一个"）→ 从 A1 开始。

禁止清单（违反即等于旧版低效行为）：

- ❌ 交互结果到达后，禁止重新通读或重跑 SKILL.md 主流程——从 `stage` 所在阶段继续。
- ❌ 禁止重复调用只读接口：`pcLogin`（token）和 `initializeData` 本会话只调一次，结果存工作记忆复用；仅当缺失或报错时才重取。
- ❌ 禁止重复询问已回答字段；禁止把已生成的提案全文重新生成一遍。
- ❌ 用户只说"把投入改成 5000"时，只增量更新 `fields.gshcxzjtr` 并重发确认卡，不重跑 A1–A3。
- ✅ 允许的"重复"：A5/A7 之后的强制回读校验、A8 台账回读——这些是写后验证，不是低效重跑。

### 0.4 交互通道：钉钉互动卡片（默认）+ 文字兜底

所有需要用户选择或填写的交互，**默认发送钉钉互动卡片**（按钮/复选/输入框/表单/markdown 卡片），不再用纯文字提问。组件选型：

| 交互内容 | 卡片组件 | 说明 |
|---|---|---|
| 是/否、二选一（如"已落地还是设想"） | `buttonList` 按钮 | 每个选项一个按钮，value 用机器可读动作码 |
| 多选（如缺失字段清单、类别拿不准） | `checkbox` 复选 | 可多选 |
| 填数字/文字（投入、收益、修改意见） | `input` / `textarea` 输入框 | 放在卡片 `form` 内，带 label 与占位符 |
| 提案全文 + 确认 | `markdown` 展示 + 底部 `buttonList` | A3/A4 确认页 |
| 每日扫描摘要 | `markdown` 摘要 + 每项一个"提报第 N 条"按钮 | 见"每日扫描"节 |

卡片 JSON（钉钉互动卡片协议，组件速查与回调约定详见 [reference.md](reference.md)）：

```json
{
  "cardData": {
    "content": [
      {"type": "markdown", "text": "### 创新提案 · 待确认\n\n…提案全文…"},
      {"type": "form", "formItems": [
        {"type": "input", "name": "gshcxzjtr", "label": "创新资金投入（元）", "placeholder": "纯数字，无投入填 0"},
        {"type": "checkbox", "name": "missing", "label": "还需我补充", "options": [{"value": "ctar", "label": "次提案人工号"}, {"value": "gshsyms", "label": "收益描述"}]}
      ]},
      {"type": "buttonList", "buttons": [
        {"text": "确认提交草稿", "value": "submit_draft"},
        {"text": "还要修改", "value": "modify"},
        {"text": "暂不提交", "value": "cancel"}
      ]}
    ]
  }
}
```

发送方式：用环境提供的钉钉机器人消息/卡片能力发送上述 JSON（如 dws 的消息发送命令）；**若环境不支持卡片（如纯网页会话），降级为 `AskUserQuestion` 或纯文本**，阶段机与流程完全不变。收到按钮回调 value 后直接查 0.5 映射。

### 0.5 交互结果 → 动作映射（卡片回调与文字答复通用）

| 收到的 value / 文字 | 动作 |
|---|---|
| `submit_draft` / "确认提交草稿" | `stage=A5` → 执行 A5，完成后发 A6 卡 |
| `modify` / "还要修改" | 收集修改意见（卡片输入框或文字）→ 增量更新 fields → 重发 A3/A4 卡 → 仍停 `A4_WAIT` |
| `cancel` / "暂不提交" | 结束，提案正文留档，不调用任何写接口 |
| `start_flow` / "直接发起创新流程" | `stage=A7` → 执行 A7，成功后 A8 |
| `keep_draft` / "先留草稿" | `stage=A8` → 仅台账留痕后结束，输出草稿 ID 与平台入口 |
| `redo` / "再改改" | 增量重渲染 A3 卡 → 仍停 `A4_WAIT` |
| `submit_N`（每日扫描卡片） | 以第 N 条为 idea 进入对话式提报（从 A2 补齐开始） |

收到交互结果：先更新 `stage`，再执行动作；不要在回复中重新解释流程或重发同一问题。

---

## 个人配置

| 项 | 值 |
|---|---|
| 星火工号 jobNumber / oaId | 首次会话时，自动获取 |
| 姓名 realName | 首次会话时，自动获取 |
| 钉钉 userId | 首次会话时，自动获取 |
| 推送机器人 robot-code | `ding29bx5qorxvglwstr`（小钉） |
| 流程 key | `cxpslc`（创新评审流程） |
| 共享台账 | baseId `Y1OQX0akWmlXvb41TjZjp2r58GlDd3mE` / tableId `hERWDMS` |

平台基址 `https://midflow.chinajack.com`。组织信息（一级/二级部门、组织分类）不写死，运行时由 `initializeData` 取，缓存进 `inn_state.initData`。

## 对话式创新提案提报

全局红线（沿用并强化）：

1. **未拿到用户点击确认之前，不得调用任何写接口**（`tempSaveExternal` / `flowOperate/save` / `flowOperate/start`）。只读接口（token、initializeData、回读、检索）不受限。
2. **草稿 ≠ 发起**。`flowOperate/start` 会真实进入审批流并给审批人推钉钉待办，必须**单独再确认一次**（A6 卡），即使上一轮已确认过草稿提交。
3. **不编造数字**。投入/收益必须是用户给过的数值；用户未表态时填平台默认值（投入 100 / 收益 1000）并在确认页标 `[默认]`，可随"还要修改"改掉；绝不按"行业均值"虚构。
4. **不创建测试草稿**。草稿删除接口被网关拦截（DELETE 返回 HTTP 000），一旦写入只能人工在平台删除。
5. token 本会话只取一次（24h 有效），存 `inn_state.tk` 复用；跨会话不复用。

### A1 收集口述（入口：无工作记忆时）

最小输入 = 一句话想法。信息不足时**只追问一轮**，用一张钉钉互动卡片把关键缺口问完（≤4 项；是/否用按钮、数值用输入框、多选用复选）。典型缺口：

- 这个改善**已经落地**了，还是**只是设想**？（决定 `sfygs`）→ 按钮
- 有没有**量化数据**：投入金额（元）、年化收益（元）？（`gshcxzjtr`/`gshsy` 必填纯数字，无经济效益填 `0`）→ 数字输入框
- 有没有**共同提案人**？（`ctar` 次提案人，需工号）→ 输入框
- 归属类别拿不准时给候选复选（见 A3 类别表）→ 复选

用户说"你看着填/都行" → 按口述自行判定，判定结果在确认页显式标注 `[自动判定]`。

收集完成 → `stage=A2`，进入 A2。

### A2 自动补齐（只读，全部缓存复用）

**① 平台预填（必做，本会话仅一次）**

```bash
TK=$(curl -s -X POST "https://midflow.chinajack.com/mid-flow/sso/pcLogin?username=40027436" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl -s -H "Authorization: Bearer $TK" \
  "https://midflow.chinajack.com/mid-flow/biz/innovationReview/initializeData"
```

返回直接映射到表单并存入 `inn_state.initData`：`zz/zzLabel`（一级部门）、`xjzz/xjzzLabel`（二级部门）、`zzfl`（组织分类）、`oaId`、`realName`、`ofOrgName`。**不要手填这些值**。

**② 钉钉检索补全（按需，非必跑）**——仅当 idea 提到具体场景/痛点且缺少背景支撑时才检索；检索不到的量化数字一律不写：

```bash
dws chat message list-all --start <ISO> --end <ISO> --limit 100 --format json   # 群聊讨论
dws minutes list all --start <ISO> --end <ISO> --format json                    # 会议（再 get summary/transcription --id）
dws doc search --query "<关键词>" --format json                                  # 文档（再 doc read --node <id>）
dws todo task list --page 1 --size 50 --format json                              # 待办
```

引用到的事实注明来源与日期（如"9/3 千问AI办公培训沟通会（IT部）提出"），存 `inn_state.evidence`。

**③ 防重（只读）**

- 台账：`dws aitable record list --base-id Y1OQX0akWmlXvb41TjZjp2r58GlDd3mE --table-id hERWDMS --format json`，比对"创新标题"。
- 我的草稿：`GET /actDefFormdata/myTempList?pageNum=1&pageSize=50&field=create_time&order=descend`，比对 `tm`。

命中相似项 → 在确认卡顶部提示"与已有记录 X 高度相似（草稿ID…/台账状态…）"，由用户决定是否继续。

完成 → `stage=A3`，进入 A3。

### A3 生成提案

按此表生成全文，**每个字段标注来源** `[用户口述]` / `[自动补齐]` / `[自动判定]` / `[需你填]` / `[占位]` / `[默认]`：

| 表单字段 | field | 必填 | 取值 | 生成要求 |
|---|---|---|---|---|
| 创新项目 | `tm` | 建议 | ≤200字 | 动宾结构、具体可识别，如"首创面板FLASH在板在线烧录方案"，不要写"创新激励"这类抽象词 |
| 类别 | `gslb` / `gslbLabel` | 建议 | 见下 | 成对写 value + label |
| 是否已改善 | `sfygs` / `sfygsLabel` | 建议 | `1`是 / `0`否 | 已落地=1，仅设想=0 |
| 创新资金投入 | `gshcxzjtr` | **是** | **纯数字**（元） | 无投入填 `0`；用户未表态默认 100 并标 `[默认]`；禁止小数/千分位/单位字符 |
| 收益 | `gshsy` | **是** | **纯数字**（元） | 无法量化填 `0`；用户未表态默认 1000 并标 `[默认]`；把非量化价值写进"收益描述" |
| 改善描述(改善前) | `gsqgsms` | 建议 | ≤200字 | 现状→痛点→代价，不超过3句话描述 |
| 改善描述(改善后) | `gshgsms` | 否 | 1–3 句 | 方案本身，一句话说清改成什么 |
| 收益描述 | `gshsyms` | 否 | 1-5 句 | 口径 + 算法 + 非量化价值 |
| 次提案人 | `ctar` / `ctarLabel` | 否 | 工号数组 / 姓名串 | 如 `["40027436"]` / `"张三"` |
| 一级/二级部门、组织分类、工号、姓名、部门 | `zz`… | 自动 | 来自 initializeData | 不覆盖 |
| 流程编号 / 发起时间 | `lcbh` / `fqsj` | — | 服务端生成 | 表单置灰，**不提交 `lcbh`** |
| 零缺陷预防组 | `lqxwtms/lqxdzhg/lqxgyfx/lqxyfcs/lqxdcxg` | 否 | 文本 | 仅质量/缺陷类提案填 |
| 建议派工人组 | `jypgr/yjbmjypgr/ejbmjypgr` | 否 | — | 仅生产派工类填，默认留空 |

类别（现行枚举，与 reference.md 一致）：`1` 质量改进类 ｜ `2` 效率提升类 ｜ `3` 制度流程创新 ｜ `4` 工具方法创新 ｜ `5` 管理创新 ｜ `7` 产品/技术创新

自评星级只在确认卡作为参考行展示（不进表单）：五星 701–1000（新材料/新技术/新产品，行业领先）· 四星 401–700（重大技术/工艺突破）· 三星 201–400（明显经济效益、多工序联合改善）· 二星 101–200（贴合业务线、标准化、安全杜绝事故）· 一星 1–100（部门痛点、简单流程梳理）· 金点子 1–10（有效建议未落实）。

写作范例见 [reference.md](reference.md)。全部字段写入 `inn_state.fields` → `stage=A4_WAIT` → 发 A4 确认卡。

### A4 第一次确认（`A4_WAIT`，卡片）

卡片内容：markdown 展示提案全文（每个字段带来源标注，缺失项标 `[需你填]`）+ form（把仍缺失的必填项做成输入框）+ buttonList（`submit_draft` / `modify` / `cancel`）。

- 收到 `submit_draft` → `stage=A5` → 执行 A5。**不得**再问一遍确认。
- 收到 `modify` / "还要修改" → 只增量更新用户指出的字段（重发确认卡，仍停 `A4_WAIT`），**不得**重新生成全文或重跑 A1–A2。
- 收到 `cancel` → 结束，正文留档。

### A5 提交到草稿（两步 + 强制回读）

含中文的 JSON 一律先 `Write` 成文件，再 `curl --data-binary @文件`，避免编码问题。

**① 建草稿**（只承载 title/gslb/goldIdea/proposal）

```bash
# body: {"jobNumber":"40027436","title":"<tm>","gslb":"4","gslbLabel":"工具方法创新",
#        "goldIdea":"<gsqgsms>","proposal":"<gshgsms 或留空>"}
curl -s -X POST "https://midflow.chinajack.com/mid-flow/biz/innovationReview/tempSaveExternal" \
  -H "Authorization: Bearer $TK" -H "Content-Type: application/json" --data-binary @/tmp/inn_draft.json
# → {"code":"1","data":"<bizId>","message":"暂存成功"}
```

`bizId` 存入 `inn_state.bizId`。

**② 补齐全字段**（投入/收益/收益描述/次提案人/是否已改善 + 组织信息）

```bash
# body: {"bizId":"<bizId>","formData":{...A3 全字段，含 xxxLabel，不含 lcbh...}}
curl -s -X POST "https://midflow.chinajack.com/mid-flow/flowOperate/save" \
  -H "Authorization: Bearer $TK" -H "Content-Type: application/json" --data-binary @/tmp/inn_form.json
# → {"code":"1","data":null}
```

**③ 回读校验（必做）**：`GET /actDefFormdata/<bizId>`，逐字段比对；草稿态应满足 `instanceId=null`、`serialCode=null`。任一必填项为空 → 报错并把缺失项列给用户（重发确认卡），**不要**继续发起。

通过 → `draftOk=true`，`stage=A6_WAIT` → 发 A6 卡。

### A6 第二次确认（`A6_WAIT`，卡片）

卡片提示"草稿已就绪 + 流程编号将为 JK-SN-… + 审批人将收到钉钉待办"，buttonList（`start_flow` / `keep_draft` / `redo`）。

- `start_flow` → `stage=A7` → 执行 A7。
- `keep_draft` → `stage=A8` → 仅台账留痕（状态"已暂存"）后结束，输出草稿 ID 与平台入口。
- `redo` / "再改改" → 增量更新后回 A3 重渲染，仍停 `A4_WAIT`。

### A7 发起流程

```bash
# ① 取当前流程定义 id（草稿里的 processDefinitionId 可能是旧版本，必须用当前值）
curl -s -H "Authorization: Bearer $TK" --get \
  "https://midflow.chinajack.com/mid-flow/common/start/page" \
  --data-urlencode "pageNum=1" --data-urlencode "pageSize=10" \
  --data-urlencode "field=create_time" --data-urlencode "order=descend" \
  --data-urlencode "key=cxpslc"          # → data.rows[0].id  形如 cxpslc:13:<uuid>

# ② 取「发起流程」按钮 id（不要写死，会随版本变）
curl -s -H "Authorization: Bearer $TK" --get \
  "https://midflow.chinajack.com/mid-flow/actNodeButton/detail" \
  --data-urlencode "taskDefinitionKey=startEvent" \
  --data-urlencode "processDefinitionId=cxpslc:13:<uuid>"
# → data 里 operate=="发起流程" 的 id  (action 应为 flowOperate/start)

# ③ 发起
# body: {"flowTitle":"创新评审流程","recordId":"<bizId>",
#        "processDefinitionId":"<①的id>","btnId":"<②的id>","formData":{...同 A5② 全字段...}}
curl -s -X POST "https://midflow.chinajack.com/mid-flow/flowOperate/start" \
  -H "Authorization: Bearer $TK" -H "Content-Type: application/json" --data-binary @/tmp/inn_start.json
```

发起后**必须**再 `GET /actDefFormdata/<bizId>` 一次：`instanceId` 非空、`serialCode`（= `lcbh`，形如 `JK-SN-YYYYMMDDNNNN`）非空即成功，把流程编号报给用户 → `stage=A8`。

> `flowOperate/start` 的报文结构逆向自平台前端（草稿箱"发起流程"入口 + FormView 提交逻辑），**尚未在本环境实跑**。首次真实执行时：先完成 A5 草稿 + 回读，再执行 ① ②，③ 若返回非 `code=="1"`，立即停止并原样回报错误信息，改走"自己去平台点提交"兜底，不要重试换参乱试。

### A8 台账留痕与汇报

提报成功后写共享台账（字段 ID 与踩坑见 [reference.md](reference.md)）：

- 新提报：`dws aitable record create --base-id Y1OQX0akWmlXvb41TjZjp2r58GlDd3mE --table-id hERWDMS --records '[{"cells":{"标题":"<tm>","创新标题":"<tm>","识别日期":"<RFC3339>","识别人":"黄景新","状态":"已暂存","星火记录ID":"<bizId>"}}]' --format json`
- A7 发起成功后：用原子 `record update` 把该记录 `状态` 改成 `已发起流程`（`+record-update` 无 `--records-file`）。
- 识别日期必须传 RFC3339 字符串（如 `2026-09-30T09:30:00+08:00`），传毫秒时间戳会被解析成 1794 年；写完回读校验。

最后向用户汇报：标题 / 类别 / 投入·收益 / 草稿 bizId / 流程编号（或"停在草稿待你提交"）/ 台账记录 ID。置 `stage=DONE`。

## 每日创新点扫描（legacy 模式）

保留原能力，仅将推送与交互改为卡片：

1. 扫描近 N 天（默认 1）钉钉聊天、会议纪要、文档、日志和待办（命令同 A2②）。
2. 按识别标准筛出未正式发布的创新火花。
3. 先比对共享台账和近 3 天推送历史去重。
4. 逐条创建星火暂存记录并写台账。
5. 用小钉推一张**互动卡片**到用户单聊：markdown 摘要 + 每条一个按钮"提报第 N 条"（value `submit_N`）。
6. 用户点击 `submit_N` → 以第 N 条为 idea，`mode=conversational`，从 A2 开始补齐并走 A4/A6 确认卡。

## 失败兜底

| 情况 | 处理 |
|---|---|
| token 获取失败 / 非 `code=="1"` | 判断是否内网不可达，直接回报，不静默 |
| `tempSaveExternal` 报"用户未绑定" | 工号格式问题，去掉前导 0 重试 |
| `flowOperate/start` 失败 | 保留草稿，输出草稿 ID + 提示去星火「我的草稿」点提交；不重复试探参数 |
| 台账无权限（403） | 提报照常，把⚠️台账权限申请提醒放在汇报最前面（所有者邱灵光，工号 157） |
| 卡片发送失败 / 环境不支持卡片 | 降级 `AskUserQuestion` 或纯文本，选项文字用 0.5 映射中的文字（如"确认提交草稿"），流程与阶段机不变 |
| 回调 value 无法识别 | 不猜测，用文字询问用户意图，并说明当前 `stage` |
| 用户中途反悔 | 已发起的流程无法接口撤回，提示在平台走撤回；仅草稿状态可直接留着 |
