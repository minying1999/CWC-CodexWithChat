# GitHub 私有仓库上传流程

这份预览包不含用户配对、运行记录和本机证据。以下是手动发布步骤，包内不声称某个账号已经登录、仓库已创建或内容已推送。

1. 在自己的 GitHub 账号下创建空的 Private 仓库，不勾选初始化文件。复制实际仓库地址。
2. 在本分发仓库根目录打开 PowerShell，确认 `git status` 与待发布提交；用真实地址替换下面的示例。

```powershell
git remote add origin 'https://github.com/YOUR_ACCOUNT/YOUR_REPOSITORY.git'
git push -u origin main
git tag --list 'v0.2.0-preview.*'
```

如已配置 origin，先检查 `git remote -v`，确认它是期望目标。需要发布标签时仅推送此次核对过的具体标签，避免批量上传历史。

登录使用 Git Credential Manager 或 GitHub 网页 OAuth。不要在仓库、脚本或消息内保存凭据。

3. 在 GitHub 核对仓库仍为 Private，远程提交与本地一致；重新克隆后运行 README 中的验证步骤。只发布本分发仓库；备份、运行数据库、咨询证据和其他工作目录保留在本地。
