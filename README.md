# hermes-serverless-sync

**用私有 Git 仓库把 Hermes Agent 在两台（或更多）电脑之间双向同步 —— 无服务器、无数据库、无需管理员权限。**

顺带解决了一件别人基本没做的事：**把会话历史（`state.db`）安全地合并过去**，而不是互相覆盖，也不是"因为怕损坏所以干脆不同步"。

> ⚠️ 非官方项目，与 Nous Research 无关。Hermes Agent 本身尚未提供跨设备同步（见 [issue #20510](https://github.com/NousResearch/hermes-agent/issues/20510)）。

---

## 它解决什么

Hermes 的所有状态都在本地（Windows 上是 `%LOCALAPPDATA%\hermes`）：

- `config.yaml`、`SOUL.md`、`skills/`、`memories/`、`sessions/`
- `.env`、`auth.json`（密钥）
- **`state.db`** —— SQLite，**所有会话的完整消息历史**都在这里（`sessions/` 目录只是遗留索引）

两台电脑各写各的，于是你换一台机器就"失忆"。本项目用一个**私有 Git 仓库做中间件**把它们连起来。

---

## 和其他方案的区别

| 方案 | 中间件 | 会话历史 | 密钥加密 | 免管理员自启 |
|---|---|---|---|---|
| **本项目** | 私有 Git 仓库（**无服务器**） | ✅ **`state.db` 只增合并**，两台的新会话都保留 | ✅ AES-256-CBC + PBKDF2 | ✅ 启动文件夹 |
| [westsource/agentctxsync](https://github.com/westsource/agentctxsync) | **自建服务端**（FastAPI + PostgreSQL + Web UI + 每机 MCP 客户端） | ✅ 走服务端 | 服务端托管 | 需部署服务端 |
| [Black-Studio-ia/hermes-sync](https://github.com/Black-Studio-ia/hermes-sync) | Git 仓库 | ❌ 不做库合并（且写死 `~/.hermes`，Windows 上失效；pull 是破坏性的） | ✅ Fernet | — |
| [TriBrigadeMars/hermes-profile-sync](https://github.com/TriBrigadeMars/hermes-profile-sync) | Git 仓库 | ❌ 明确不同步 sessions | ❌ | ✅ |
| 云盘软链 SOUL/MEMORY（社区常见做法） | 云盘 | ❌ 明确"会损坏，放弃" | — | — |

**本项目的核心新意**：对 `state.db` 做**无服务器、只增、可重复**的合并（见下）。

---

## 工作原理

### 1) 文件同步（`-Mode push / pull / both`）

只同步一个**白名单**（其余如 `tools/`、`bin/`、`cache/` 可能好几个 GB，绝不动）：

```
config.yaml, SOUL.md, channel_directory.json
skills/ , memories/ , sessions/
```

- `push`：白名单内容复制进工作副本的 `payload/` → 加密密钥 → 提交 → `git pull --rebase` → `git push`
- `pull`：`git pull` → 把 `payload/` **非破坏性**地拷回（覆盖/新增，**从不删除**本地文件）
- 冲突策略：**后写者覆盖**（不追求两机逐字节相同）

### 2) 加密密钥

`.env` 和 `auth.json` 用 **AES-256-CBC + PBKDF2-SHA256（10 万次）** 加密后进仓库（`secrets/*.enc`）。
密码来自环境变量 `HERMES_SYNC_PASSPHRASE`，两台必须一致；丢了就解不回来。**仓库必须私有。**

### 3) 会话历史合并（`-Mode sessions`）—— 本项目的关键

会话在 `state.db`（SQLite/WAL）。直接复制活动数据库会拿到损坏镜像，互相覆盖又会丢会话。这里的做法：

1. **快照**：用 SQLite 在线备份 API 生成一致性副本（可在 Hermes 运行时进行），写成 `snapshots/<标签>.db`。
2. **传输**：快照走一个**独立的单提交分支 `sessions-snapshot`，每次强制推送** —— 所以仓库**永不膨胀**（每台机器一个文件，13MB 覆盖式更新，不进历史）。
3. **合并**：**只增**。
   - 会话按 `sessions.id`（TEXT 主键，全局唯一）去重，本地已有的**跳过**，绝不修改已有行；
   - 消息**不带 `id` 插入**，由 `AUTOINCREMENT` 分配新 ID —— Hermes 自带的触发器会自动维护 `messages_fts`（FTS5 全文索引）、`message_uid`、`display_order`，所以**索引不需要手工重建**；
   - 顺带搬 `system_prompts`（按 hash）和 `session_model_usage`；
   - **跳过**机器本地状态（`gateway_routing`、`delivery_obligations`、`session_turn_leases` 等）。
4. **分类**：并进来的会话自动加来源前缀 —— `[台式机] 淘宝选购日常商品`、`[笔记本] …`。标签名用 `HERMES_SYNC_LABEL` 自定义（默认计算机名）。
5. **跨版本保护**：若本地库比快照缺少列（Hermes 版本不一致），**直接拒绝合并**并报错，不做猜测。
6. 合并前用 SQLite 在线备份留一份 `state.db.bak-<时间戳>`。

### 4) 自动运行（Windows，免管理员）

`Task Scheduler` 注册常被非管理员进程拒绝，所以用**启动文件夹**：登录时启动一个隐藏循环——

- 每 **30 分钟**：配置 / 技能 / 记忆（`both`）
- 每 **6 小时**：会话（`sessions`）；失败（断网等）**每 30 分钟自动重试**，成功后才回到 6 小时节奏

---

## 要求

- **Windows** + Windows PowerShell 5.1+（脚本刻意只用 ASCII，避免 PS 5.1 把无 BOM 的 `.ps1` 按 GBK 解码而炸掉）
- **git**（Git for Windows）
- **Python 3.8+** —— 脚本会自动找解释器：PATH → `%LOCALAPPDATA%\hermes\tools\python-*` → Hermes 安装目录的 venv，并**跳过 Windows 商店的假别名**（`WindowsApps\python.exe` 是个跑不出东西的空壳）
- 一个**私有** Git 仓库（GitHub / GitLab / 自建都行）

---

## 快速开始

```powershell
# 1) 三样东西存成用户环境变量（自动运行时要用）
[Environment]::SetEnvironmentVariable('HERMES_SYNC_REMOTE',  'https://github.com/<you>/<repo>.git', 'User')
[Environment]::SetEnvironmentVariable('HERMES_SYNC_PASSPHRASE', '<强密码>', 'User')
[Environment]::SetEnvironmentVariable('HERMES_SYNC_LABEL',    '笔记本', 'User')   # 可选，中文标签

# 2) 把 4 个文件放到
#    %LOCALAPPDATA%\hermes\sync\

# 3) 首次同步（会弹一次 GitHub 登录，之后凭据缓存）
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\hermes\sync\sync-hermes.ps1" -Mode push

# 4) 另一台机器
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\hermes\sync\sync-hermes.ps1" -Mode pull

# 5) 会话（把本机快照推上去，并并入别的机器的；可随时手动跑）
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\hermes\sync\sync-hermes.ps1" -Mode sessions

# 6) 自动运行：把 HermesSync.cmd 放进启动文件夹（Win+R → shell:startup）
```

**如果 git 连不上 GitHub**（常见于系统代理环境）：Clash/V2Ray 之类的系统代理 git 默认不走，需要：
```powershell
git config --global http.proxy http://127.0.0.1:7890   # 换成你的代理端口
git config --global https.proxy http://127.0.0.1:7890
```

---

## 验证

`state_db_tool.py` 可以独立验证，不碰你的真实数据：

```bash
python state_db_tool.py snapshot --out snap.db          # 一致性快照
# 把 snap.db 复制成 state.db 放进一个空目录，删掉几个会话模拟"缺数据的机器"
python state_db_tool.py merge --snapshot snap.db --home <那个目录> --tag DESKTOP
# 断言：会话数/消息数与快照完全一致；FTS 完整性检查通过；Hermes 能列出合并后的会话
```

作者实测：删掉 3 个会话（181 条消息）→ 合并 → 精确复原 11 会话 / 2001 消息，FTS `integrity-check` PASS，`hermes sessions list` 正常读取。

---

## 已知限制（诚实版）

- **只测了 Windows**。路径解析走 `HERMES_HOME` / `%LOCALAPPDATA%`，其他平台需要改路径假设。
- **文件同步是后写者覆盖**，不是行级合并。
- **会话合并依赖 Hermes 的 `state.db` 表结构**。表结构变化时脚本会拒绝而不是搞坏数据，但需要跟着更新。
- 合并进来的会话**可能要在 Hermes 里刷新/重启**才出现在列表中（网关有内存缓存）。
- 中途 `state.db` 的快照是**整库**（含系统提示等），约十几 MB —— 走独立分支强推，所以仓库不涨，但每轮传输是这个量级。
- 不处理 Hermes 的其它机器本地库（`kanban.db`、`cron/executions.db`）——活动 SQLite 一律不碰。

---

## 文件

| 文件 | 作用 |
|---|---|
| `sync-hermes.ps1` | 主引擎：`push` / `pull` / `both` / `sessions`，AES 加解密、白名单、快照分支 |
| `state_db_tool.py` | 会话库工具：`snapshot`（在线备份）/ `merge`（只增合并，带来源标签） |
| `sync-loop.ps1` | 隐藏后台循环（30 分钟文件 / 6 小时会话 + 失败重试） |
| `HermesSync.cmd` | 启动文件夹启动器 |

---

## 许可

MIT。见 [LICENSE](LICENSE)。

**免责声明**：非官方工具，可能随 Hermes 升级失效；合并的是你的真实会话库，请先确认 `state.db.bak-*` 备份存在再放心使用。
