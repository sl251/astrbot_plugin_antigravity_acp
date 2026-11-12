# v0.4.1 测试记录

日期：2026-11-12。本版改进 CLI 路径与 ACP 启动错误诊断，不自动修改部署配置。

## 离线回归

本地 Windows、Python 3.11.15 / 3.12.10、Node.js 24.11.1：每个 Python 版本执行 88 项测试，86 通过、2 项平台/权限检查跳过；页面交互测试 10 项通过。保留 v0.4.0 全部回归。

新增覆盖：

- 默认搜索找不到 CLI 与显式路径无效使用不同类别；显式配置不回退、不在错误中回显私人路径。
- 页面显示搜索模式、失败阶段和退出码（包括 0），三类启动故障分别提示；缺 CLI 时仍禁止保存模型。
- 假 CLI 的真实 stdio 子进程：握手前退出 0/23、逐字 stderr 旧脚本拒绝、已知网络错误优先、握手后退出仍为 TRANSPORT、stderr 告警不否决成功。
- stdin 写失败先于 stdout EOF 时仍能收集有界分类证据；只关闭输出管道但进程仍在运行时不虚报“进程退出”。
- 显式 RPC 认证错误优先，旧阶段 stderr 不污染新阶段；错误信息不保留原始日志、私人路径或测试秘密。
- CLI 文件存在但启动时报 FileNotFoundError，按解释器/加载器问题归类 LAUNCH；版本/能力查询失败保留退出码并回收进程。

两项本地跳过为 POSIX 权限位检查、Windows 正向文件桥安全元数据/链接校验；沿用 v0.4.0 的平台边界。CI 运行 Linux/Windows × Python 3.11/3.12，Node 22，具体结果见对应提交的 [GitHub Actions](https://github.com/sl251/astrbot_plugin_antigravity_acp/actions)。

## 实机无生成验证

在现有 Debian Docker 部署中，通过独立临时源码副本调用新版 probe，复用现有插件设置，仅在测试内指定不同 command；未修改安装中的插件、配置、包装脚本或持久凭据，未重启服务。临时副本已清理。

| CLI 入口 | 结果 |
| --- | --- |
| 旧部署包装脚本（版本查询可成功） | CLI 1.5.4，initialize 阶段阻断，`CLI_WRAPPER`，退出码 1；不再只返回 TRANSPORT |
| 官方原生可执行文件 | CLI 1.5.4，认证、ACP 握手、建会话及模型目录通过，返回 4 个模型 |

两次检查的 `model_prompt_sent` 均为 false，不发送真实生成请求。本次验证没有重新登录、读取或输出 token 内容。

## 复跑

```sh
python -m unittest discover -s tests -v
node --test tests/setup.test.mjs
python -m compileall -q main.py agy_provider tests
git diff --check
```

真实 WebUI 截图、Google 交互登录、真实生成、完整工具/图片、多平台认证和长期刷新仍未全量验收。实机 probe 成功不能替代这些验证。
