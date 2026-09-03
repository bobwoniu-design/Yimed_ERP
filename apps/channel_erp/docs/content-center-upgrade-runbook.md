# 内容中心升级与回滚手册

适用范围：`channel_erp` 内容中心，当前验证基线为 Frappe `16.31.0`、ERPNext
`16.32.0`。内容中心只通过自定义应用扩展系统，不修改 Frappe 或 ERPNext 核心文件。

## 一、升级原则

- 先在测试或预发布站点升级并完成回归，再安排生产升级；
- 数据库、公开文件、私有文件和站点配置必须作为同一批备份保存；
- 不在生产站点运行集成测试；测试会创建临时业务数据；
- 不使用 `git reset --hard`、`git checkout --` 等方式自动清理核心仓库；
- Frappe 或 ERPNext 跨主版本升级时，先按不兼容升级处理并重新验收。

## 二、升级前检查

以下命令均在 Bench 根目录执行，示例站点名为 `yimed.local`。

1. 检查 Frappe、ERPNext 核心仓库是否存在受版本控制的本地修改：

   ```bash
   python3 apps/channel_erp/scripts/check_core_immutability.py
   ```

   检查失败时先确认修改的来源和处置方案，不允许脚本自动覆盖。当前环境已检测到
   `apps/frappe/banking/yarn.lock` 存在既有修改，正式升级前需要由维护人员确认。

2. 执行内容中心升级巡检：

   ```bash
   bench --site yimed.local execute \
     channel_erp.upgrade_checks.run_upgrade_checks \
     --kwargs '{"fail_on_error": 1}'
   ```

3. 生成包含站点配置、数据库、公开文件和私有文件的压缩备份：

   ```bash
   bench --site yimed.local backup --with-files --compress
   ```

4. 记录当前三个应用的分支和提交号，并将备份复制到 Bench 之外的受控存储。

也可以运行统一的只读预检入口；只要核心仓库有本地修改，它就会立即失败：

```bash
bash apps/channel_erp/scripts/content_center_preflight.sh yimed.local
```

## 三、预发布升级与验收

1. 用生产备份建立隔离的测试站点；
2. 将 Frappe、ERPNext 和 `channel_erp` 切换到计划部署的明确版本；
3. 执行迁移并重建前端资源：

   ```bash
   bench --site staging.local migrate
   bench build --app channel_erp
   bench --site staging.local clear-cache
   ```

4. 再次执行升级巡检；
5. 仅在该测试站点开启测试并运行内容中心回归：

   ```bash
   bench --site staging.local set-config allow_tests true
   bash apps/channel_erp/scripts/content_center_regression.sh staging.local
   bench --site staging.local set-config allow_tests false
   ```

6. 人工验收以下关键路径：进入 Desk 内容中心、目录权限、上传与预览、新版本、
   回收与恢复、关联物料、物料附件可见、从物料侧删除附件后的关系同步。

## 四、生产升级

在维护窗口内暂停业务写入，重新生成一份完整备份，再部署已经在预发布环境验证过的
三个应用版本。随后依次运行：

```bash
bench --site yimed.local migrate
bench build --app channel_erp
bench --site yimed.local clear-cache
bench --site yimed.local execute \
  channel_erp.upgrade_checks.run_upgrade_checks \
  --kwargs '{"fail_on_error": 1}'
```

巡检通过后完成人工冒烟测试，再恢复业务访问。不要在生产站点开启 `allow_tests`。

## 五、失败回滚

如果迁移或验收失败：

1. 保持业务入口关闭并保存错误日志；
2. 将三个应用恢复到升级前记录的提交；
3. 从同一批备份恢复数据库、公开文件和私有文件：

   ```bash
   bench --site yimed.local restore /path/to/database.sql.gz \
     --with-public-files /path/to/public-files.tar \
     --with-private-files /path/to/private-files.tar
   ```

4. 恢复对应的站点配置和加密密钥，执行升级前版本的 `migrate`、资源构建和巡检；
5. 人工验证登录、业务单据附件和内容中心文件后再恢复访问。

恢复命令中的路径必须指向同一时间生成的一组备份；加密密钥不一致会导致加密字段无法读取。

## 六、长期兼容关注点

- `File` 的 `on_trash`、`after_delete` 事件及附件字段语义；
- Desk 页面和侧栏 DOM/CSS 结构；
- 文件上传、私有文件下载和表单附件接口；
- Frappe 数据查询参数的弃用提示；
- 每次 Frappe/ERPNext 升级后的内容中心巡检、自动回归和人工附件联动验收。
