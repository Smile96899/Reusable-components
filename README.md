# deploykit

通用的多服务器发版工具。整个工具就是一个 Python 文件，只依赖标准库，任何项目都能用。每个项目只需在自己的仓库里放一份 `deploy.toml`。

- **本地**：Python 3.11+、git、ssh/scp（Windows 自带 OpenSSH，另需装 Git for Windows）
- **服务器**：bash、tar、sha256sum、cmp、curl。SQLite 在线备份需要服务器上有 python3 或 sqlite3。

## 一次 `deploy` 做了什么

```
本地  git 检查（干净 / 分支 / 已推送）→ steps（测试、构建）→ 按 include/exclude 打包 + sha256
每台  加锁 → 上传 → 校验 sha256 → 解包到 stage → require 文件检查 → verify 命令
      → 备份（SQLite / MySQL / 自定义）→ 先写好 rollback.sh → 激活 → 重启 → 检查
      → 成功：清理旧版本（保留 keep_releases 个）→ 解锁 → 下一台
      → 失败：激活前失败则线上没有任何改动；激活后失败则自动执行 rollback.sh、重启、复查，并停止后续服务器
```

多台服务器按 `[[targets]]` 的顺序逐台滚动发布，前一台没通过检查就不会动下一台。

### 两种发布方式

| mode | 行为 | 适合 |
|---|---|---|
| `overlay` | 只覆盖内容有变化的文件；服务器上的其他文件（`.env`、数据库、uploads）不动；被覆盖的旧文件会备份，新增的文件会记录下来 | 后端源码目录 |
| `swap` | 新内容先放进 `<dest>-next-<id>`，检查 `require` 后原子切换；旧目录保留为 `<dest>-backup-<id>` | 前端 `dist`、任何整目录替换的构建产物 |

`swap` 永远把 `src` 目录的**内容**作为 `dest` 的内容，不会出现 `dist/dist/index.html` 这种目录多套一层的问题。

### 服务器上的目录结构

```
<releases_dir>/
  .lock/                      发版锁（防止两个人同时发）
  20261028-151418265-ab12cd34/
    manifest.json             git commit、发布人、时间、每个包的 sha256
    backup/                   本次发版前的数据库备份 + integrity/记录数
    overlay/<name>/           被覆盖的旧文件
    overlay/<name>.changed    变更文件列表
    overlay/<name>.new        新增文件列表
    rollback.sh               本版本的回滚脚本
    ACTIVE / FINISHED / ROLLED_BACK
```

## 在项目里使用

1. 把本仓库放到固定位置，或作为 git submodule 引入：
   ```sh
   git submodule add <本仓库地址> tools/deploykit
   ```
   也可以安装成命令：`pip install git+<本仓库地址>`，之后直接用 `deploykit ...`。
2. 复制 [examples/deploy.example.toml](examples/deploy.example.toml) 到项目根目录，命名为 `deploy.toml` 并按项目修改。这个文件可以提交，但**不要写入任何密钥**。SSH 认证请用 `~/.ssh/config` 或 `identity` 字段。
3. 常用命令：
   ```sh
   python tools/deploykit/deploykit.py package --list        # 看会发哪些文件（确认没带 .env/数据库）
   python tools/deploykit/deploykit.py deploy --dry-run      # 打印要在服务器上执行的全部脚本
   python tools/deploykit/deploykit.py deploy                # 发布到所有 targets
   python tools/deploykit/deploykit.py deploy -t node-a      # 只发一台
   python tools/deploykit/deploykit.py status                # 各服务器的发布记录
   python tools/deploykit/deploykit.py rollback -t node-a    # 回滚最新的生效版本（可连续执行，逐个往回退）
   python tools/deploykit/deploykit.py unlock -t node-a      # 发版进程被强行中断后，清掉残留的锁
   ```

完整字段说明见 [examples/deploy.example.toml](examples/deploy.example.toml)，真实项目的配置示例见 [examples/iconiq-canada.toml](examples/iconiq-canada.toml)。

## 健康监控与告警

```sh
python deploykit.py install-monitor -t node-a
```

这条命令会在服务器上安装 `deploykit-monitor`，并配一个 systemd timer 每分钟跑一次。故障时发一次告警，恢复时再发一次，不会反复刷屏。安装后需要在服务器上编辑两个文件：

- `/etc/deploykit/monitor.conf`：节点名、Telegram 或 Webhook 地址（支持企业微信、钉钉、飞书、Slack）
- `/etc/deploykit/checks.list`：检查项，每行一条，支持 `http` / `systemd` / `disk` / `tcp` / `cmd` 五种类型

配完后用 `deploykit-monitor --test` 立即跑一遍检查，并发一条测试消息。

## 不做的事（请注意）

- **数据库不会回滚**。回滚只恢复代码和文件，每次发版前的数据库备份放在 `release/backup/` 里，需要的话手动恢复。
- **不跑数据库迁移**。schema 变更请单独评审，或者自己写到 `hooks.before_activate` 里，并清楚它的后果。
- **不是 HA 方案**。它解决的是"同一份代码可靠地发到多台机器"。多台机器之间共享数据或主备切换，取决于项目自己的数据层（例如 MySQL 主从）。
- `overlay` 不会删除服务器上的旧文件。本地删掉的源码文件需要手动清理，或者改用 `swap`。

## 开发

```sh
python -m unittest discover -s tests -v
```

端到端测试用 `host = "local"` 在本机 bash 里完整跑一遍：发布、更新、回滚、检查失败自动回滚、激活前失败、旧版本清理、发版锁。
