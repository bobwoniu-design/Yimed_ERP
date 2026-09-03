# ERPNext Yimed Monorepo

本仓库是医麦德 ERPNext v16 开发环境的完整源码快照，包含：

- `frappe`
- `erpnext`
- `channel_erp`
- `yimed_ecommerce`

四个应用由本仓库统一进行版本管理。`apps/` 内没有嵌套的 `.git`，Windows
开发环境通过符号链接直接使用这些目录，因此在 WSL2 中修改的代码可以由本仓库统一提交。

## 安全边界

本仓库只保存代码，不保存业务数据或运行凭据。以下内容不得提交：

- 数据库备份
- 公共、私有附件
- `site_config.json`
- `encryption_key`
- 平台 API Key、密码和 Token
- Bench 的 `env`、`sites`、`logs` 和 `node_modules`

业务数据应使用 Bench 生成备份，再通过加密移动硬盘、加密压缩包或可信的私有文件通道传输。

## 1. 在 Mac 上生成迁移数据

建议先停止新增业务，并关闭调度器，避免迁移期间继续同步平台订单：

```bash
cd /Users/mac/frappe-bench
bench --site yimed.local set-maintenance-mode on
bench --site yimed.local disable-scheduler
bench --site yimed.local backup --with-files --compress
```

备份生成在：

```text
/Users/mac/frappe-bench/sites/yimed.local/private/backups/
```

需要安全带到 Windows 的文件包括数据库、公共附件、私有附件和
`site_config_backup.json`。不要把这些文件添加到本仓库。

## 2. Windows 安装 WSL2

使用管理员 PowerShell：

```powershell
wsl --install -d Ubuntu
```

重启 Windows，进入 Ubuntu 完成用户名设置。建议把仓库克隆到 WSL 的 Linux
文件系统，不要放在 `/mnt/c`：

```bash
mkdir -p ~/src
cd ~/src
git clone https://github.com/bobwoniu-design/erpnext_Yimed.git
cd erpnext_Yimed
```

## 3. 一键安装开发环境

```bash
chmod +x scripts/*.sh
./scripts/setup-wsl.sh
```

也可以指定 Bench 路径：

```bash
./scripts/setup-wsl.sh ~/work/frappe-bench
```

脚本会安装当前源码要求的 Python 3.14、Node.js 24+、MariaDB、Redis、Bench
5.31.0 和所有构建依赖，然后将 `apps/` 链接到新 Bench。

> 本仓库统一管理四个应用，因此不要在生成的 Bench 中执行 `bench update`。
> 上游 Frappe/ERPNext 升级应先合并到本仓库，再执行 `bench setup requirements`
> 和 `bench --site yimed.local migrate`。

## 4. 一键恢复数据

假设备份位于 `/mnt/c/Users/你的用户名/Downloads/yimed-backup`：

```bash
./scripts/restore-site.sh \
  /mnt/c/Users/你的用户名/Downloads/yimed-backup
```

可选参数顺序：

```text
restore-site.sh <备份目录> [Bench目录] [站点名]
```

恢复脚本只从旧配置中合并 `encryption_key`、应用列表和开发设置，不会覆盖
Windows 新环境的数据库地址、端口、数据库名或密码。恢复完成后调度器保持关闭，
以免 Mac 和 Windows 同时执行渠道同步。

## 5. 启动与验证

```bash
./scripts/start-dev.sh
```

浏览器访问：

```text
http://localhost:8000
```

验证应用和站点：

```bash
cd ~/frappe-bench
bench --site yimed.local list-apps
bench --site yimed.local doctor
```

确认用户、公司、商品、订单、附件和渠道连接都正确后，再开启调度器：

```bash
bench --site yimed.local enable-scheduler
bench --site yimed.local set-maintenance-mode off
```

旧 Mac 环境应保持关闭或禁用调度器，避免重复拉单、库存回传和其他渠道任务。

## 源码版本

初始快照来源及基础提交见 [`config/source-versions.json`](config/source-versions.json)。
该快照包含基础提交之后尚未提交的本地修改，因此恢复环境应以本仓库内容为准。
