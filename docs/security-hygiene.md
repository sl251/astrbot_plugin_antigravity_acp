# 仓库安全卫生（issue #18 配套）

## 背景

历史上 `docs/archive/review-bundle/429-findings.md`（原仓库根目录 `review-bundle/429-findings.md`）曾包含一次实机验收的账号标识与认证元数据
（`authMethod` / `quotaProject` 空值等）。公开记录已通过以下动作清理：

1. `main` 脱敏提交（#18 前置提交，替换为不含账号信息的说明句）；
2. Git 历史重写：所有含旧文件的提交与标签已重写，v0.6.0 / v0.6.1 的 Release ZIP
   同步重新打包替换；发布功能、版本号与安装方式不变（tag 目标 SHA 变化）。
3. 复扫确认：全部活动 refs、历史快照与 Release 资产均不含原邮箱模式。

## 提交前扫描

```sh
python tools/check_sensitive.py            # 全仓库
python tools/check_sensitive.py --staged   # 仅扫描 Git 暂存区变更文件（可挂载 pre-commit）
python tools/check_sensitive.py 文件       # 指定文件
```

规则：邮箱、中国大陆手机号、常见 API Key 与 Google OAuth 令牌形态（`ya29.` / `1//` / `gh[pousr]_` 等）、内嵌账密的代理 URL、认证元数据字段（带值的 `authMethod` /
`quotaProject` / `access_token` 等）。命中返回非零退出码，CI 已接入该检查。
白名单只放通用占位符（`example.com`、`fake-password` 等），不放真实账号。

## 归档分发排除（`.gitattributes`）

仓库根目录的 [`.gitattributes`](../.gitattributes) 将 `tests/`、`docs/`（含 `docs/archive/`，用 `/docs/**` 覆盖嵌套路径）、`tools/`、`.github/` 及 `AGENTS.md` 标记为 `export-ignore`，确保用户下载 GitHub `main.zip` 或 Release 源码包时仅包含插件运行时所需文件，不影响 `git clone` 与 CI 回归。

## docs/archive/review-bundle/ 的定位

`docs/archive/review-bundle/`（原位于仓库根目录）是历史审查材料（PR9 diff、验收笔记、错误样本），保留其公开技术
结论；其中的账号相关行已脱敏。该目录不属于插件运行代码，**不要**把新的日志、
会话快照或真实账号信息放进该目录；如需补充材料，先脱敏再提交。

后续可选（尚未执行）：

- 把 `docs/archive/review-bundle/` 移出公开仓库，改为发布脱敏摘要；
- 若 GitHub 侧仍缓存旧对象（PR refs、旧 SHA 直达链接），按 GitHub 敏感数据清理
  流程联系 Support 处理。

## 协作者注意

历史重写后所有旧克隆的 `main` / `tag` 与本仓库不再一致，需要重新 clone（或
`git fetch --force --tags` 后 `git reset --hard origin/main`），不要直接 merge 旧历史。
