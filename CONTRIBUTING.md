# 参与贡献

## 本地开发

```bash
pip install -r requirements-dev.txt
python -m pytest
ruff check . && ruff format --check .
```

测试不需要安装 AstrBot：`core/` 下的逻辑与框架无关；`tests/test_plugin.py` 用桩模块加载 `main.py` 和 `web_api.py`。

在真实 AstrBot 中调试：把仓库目录放到 `data/plugins/` 下，在 WebUI 的插件页重载插件。修改 `pages/` 下的静态文件只需刷新面板。

## 日志与调试约定

- 所有日志以 `[deepdeep]` 开头，与某次调用相关的带上请求 ID：`#a1b2c3`
- 级别：每次调用的细节用 DEBUG；状态变化（不可用 / 恢复、熔断、降级、设置变更）用 INFO，且只在变化时记一次；调用失败用 WARNING
- 绝不记录 API Key；搜索词、链接等用户内容只在 DEBUG 级别出现，并遵循 `record_queries` 开关
- 工具结果第一行固定为 `status_line()` 的格式，新增结果类型时也要遵守
- 需要给用户展示的排查信息写进 `ActivityLog` 的 `detail`，工具结束时会随调用日志一起持久化，面板会自动展示
- 调用日志（`core/calllog.py`）的方法是同步的，在异步代码里一律用 `asyncio.to_thread` 调用；记录失败只记 WARNING，绝不能影响工具调用本身

## 兼容性检查

`.github/workflows/compat.yml` 每周一（北京时间 10:00）用最新的 AstrBot 正式版自动检查：

1. 跑一遍单元测试
2. 在 AstrBot 官方 Docker 镜像里运行 `scripts/compat_probe.py`，逐项检查插件依赖的接口（工具注册、各个钩子、`astrbot.api.web`、插件页面目录、钩子触发顺序等）
3. 挂载插件真正启动一次 AstrBot，确认插件加载成功、两个工具都注册了、没有相关报错

不通过时会自动创建标签为 `compat` 的 issue；同一个版本已有未关闭的 issue 时改为追加评论。镜像还没发布时本周跳过。

也可以在 GitHub 的 Actions 页面手动运行，指定任意版本（如 `v4.26.0`），勾选 dry run 时只在日志里输出 issue 内容。本地运行：

```bash
bash scripts/compat_check.sh v4.29.1
```

AstrBot 改了插件依赖的接口时，按报告修复后，记得同步更新 `metadata.yaml` 的 `astrbot_version` 和 README 里的版本要求。

## 提交规范

提交信息使用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)，例如 `feat: 支持每周费用上限`、`fix: 熔断后未恢复`。

## 发布

1. 同时修改 `metadata.yaml` 的 `version` 和 `main.py` 的 `PLUGIN_VERSION`
2. 在 `CHANGELOG.md` 顶部写好该版本的说明（标题格式 `## vX.Y.Z（日期）`）
3. 合并到 `main` 后，Release 工作流会自动打 tag 并发布
