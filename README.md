1. git        `E:\workspace\myown\SpecPatchGuard`

```powershell
.\scripts\push_to_github.ps1 -Branch main -CommitMessage "Initial upload"       
```

`-Branch main` & `-CommitMessage ...` 为可选参数

```powershell
# 1. 简洁查看提交历史（推荐）
git log --oneline --graph --decorate -n 20

# 2. 查看每次提交改了哪些文件、增删行数
git log --stat -n 10

# 3. 查看某一次提交的具体代码改动（把 <commit_id> 换成如 d383511）
git show <commit_id>

# 4. 查看哪些本地提交还没有推送到 GitHub
git log origin/master..HEAD --oneline

# 1. 查看当前修改状态
git status

# 2. 把当前改动加入暂存区（会自动跳过 .env 和 push_to_github.ps1）
git add -A
# 如果只想提交指定文件，也可以写：git add PLAN.md specpatchguard/xxx.py

# 3. 仅在本地生成一条新的 Git 历史记录
git commit -m "填写本次修改说明"
```









## todo

config.json中的docker路径需要修改
