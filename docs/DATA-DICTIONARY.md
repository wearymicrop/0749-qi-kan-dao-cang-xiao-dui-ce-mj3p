# 数据字典

## 输入清册

输入为 UTF-8 JSON 对象，字段 `rules`、`titles`、`receipts` 分别为三张数组。对应单表上限 3000 行，生成应到期位上限 10000。原始行号从 `receipts` 首个数组项起算，首项为 1。

| 表 | 字段 | 定义 |
|---|---|---|
| 订购 | `chain` | 稳定订阅链字符串，区分大小写 |
| 订购 | `from_year`, `to_year` | 双端包含，整数 1900–2200，单段不超过 12 年 |
| 订购 | `first_volume` | 首年卷号，随后每年加一 |
| 订购 | `issues_per_year` | 单年正刊期数 1–60 |
| 订购 | `numbering`, `first_issue` | `yearly` 按年从 1 起，`continuous` 按出版年度累加，首年首期可指定 |
| 刊名 | `chain`, `title`, `from_year`, `to_year` | 生效年度包含两端，规范化后同刊名每年只有一个映射，同链可登记同年的多个别名 |
| 来刊 | `accession` | 物理件验收号，以字符串原样保留前导零，去除两端空格后参与去重 |
| 来刊 | `title`, `year` | 来刊登记刊名和整数出版年，年度按出版年核对 |
| 来刊 | `volume`, `issue` | 字符串，允许全角与前导零，卷期为 1–9999 |
| 来刊 | `kind` | `regular` 正刊默认值，`supplement` 增刊单列 |

期号范围可使用 `-`、`–`、`—`、`~` 或 `至`，包含两端。来刊以验收号分组，同组相同字段视为重放，不同字段保留独立候选。`null`、空字串或空对象记空行，其他坏字段给出字段名并随原值隔离。不使用到达顺序推断卷期。

## 期位核验

期位键为 `(chain, year, issue)`。卷号由订购规则唯一决定，用于来刊交叉检查。相邻订购段须递增接续卷号，两段都连排时还须接续期号。合期只认合法年度内的完整范围。每个期位通过所有候选确定状态，分为覆盖、缺刊、争议。争议包括多个件号覆盖和同件号不同来刊描述。两种争议均可从到藏记录追溯原始清册。

## 调用与归档

各阶段接受 `GET /api/<阶段>/<clean|dirty>` 或 `POST /api/<阶段>`。请求体用 `{"sample":"dirty"}` 或 `{"data":{...}}`，两个来源只能选一个。阶段名为 `orders`、`receipts`、`numbering`、`combined`、`titles`、`balance`。

`POST /api/runs` 接受同一请求并保存运行。运行编号为规则版本与规范 JSON 输入摘要的 SHA-256。`GET /api/runs/{id}` 读归档，`POST /api/runs/{id}/rerun` 用归档里的原输入再算。同摘要的重复归档保留首份运行时间与结果。规则版本变化会生成新编号。`GET /api/runs/{id}/export` 下载一份 JSON 凭证，含原始清册和计算证据，归档时间用 UTC。

## 停复刊交班（M8）

### 来函输入

`POST /api/handover/batches` 请求体为 `{"rules": [...], "notices": [...], "note": "..."}`。`rules` 仅首批必附，结构同订购表；后续批次省略时沿用首批锁定的规则，避免交班中途换口径。来函字段：

| 字段 | 定义 |
|---|---|
| `letter_no` | 出版方来函编号，去空格后参与同号冲突判定 |
| `kind` | `suspension` 停刊函 / `resumption` 复刊函 |
| `effect` | 仅停刊函有效：`pause` 暂停出刊（期位挂"仍待确认"）/ `terminate` 终止出版（期位挂"停刊"），默认 `pause` |
| `chain`, `year` | 订阅链与整数出版年，须落在订购段内 |
| `volume` | 字符串卷号，允许全角与前导零，须与订购规则推算卷号一致 |
| `issue_first`, `issue_last` | 覆盖期位范围（含两端），`issue_last` 省略时视为单期；越出本卷范围整函隔离 |
| `title`, `issued_on` | 可选备注，随依据链保留 |

### 台账与幂等

每件来函先写 `handover_inbox`（独立事务，失败不回滚原件），再校验。幂等键为编号、类型、效力、链、年、卷、期范围的规范 JSON 摘要：完全相同的来函重跑只在 `handover_replays` 记一条重放（含原台账序号），不重复增减期位。同编号但内容不同的来函判 `LETTER_CONFLICT` 隔离，不覆盖先前来函。复刊函与任何在案停刊范围无交集时判 `RESUMPTION_WITHOUT_SUSPENSION` 隔离。空行、坏字段、越界、错链分别记 `EMPTY_ROW`、`NOTICE_SCHEMA`、`OUTSIDE_RANGE`、`OUT_OF_ORDER`、`WRONG_VOLUME`。

### 清单版本与接口

期位状态为 `suspended`（停刊）、`resumed`（已恢复）、`pending`（仍待确认），每格附完整依据链 `basis` 与最新来函编号。快照摘要覆盖整份台账（采信、隔离、重放、原件），任一处变化即产生新版本；内容无变化（如空批）复用现有版本。

| 接口 | 用途 |
|---|---|
| `POST /api/handover/batches` | 送达一批来函，返回本批采信/隔离/重放计数与最新清单 |
| `GET /api/handover/current` | 当前可交班版本；补件发布失败时仍返回旧版 |
| `GET /api/handover/versions` | 版本列表与当前版摘要 |
| `GET /api/handover/versions/{version}` | 调取任一历史版本 |
| `GET /api/handover/ledger` | 来函台账：全部原件、采信函、隔离原件、重放记录、锁定规则 |
