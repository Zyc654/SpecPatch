# SpecPatchGuard 服务器部署修改清单（SERVER_DEPLOY_CHECKLIST）

本文档记录将 `SpecPatchGuard` 从 Windows 本地开发机上传到 **Linux 服务器** 后必须核对与修改的所有配置项、路径映射与启动自检命令，防止遗漏。

---

## 1. 目录结构与上传范围

项目按三篇参考论文的分层包规范组织，上传时请按相对结构同步到服务器目标工作目录（例如 `/data/patch_va/`）：

```text
/data/patch_va/
├── myown/
│   └── SpecPatchGuard/
│       ├── .env.example
│       ├── pytest.ini
│       ├── SERVER_DEPLOY_CHECKLIST.md
│       ├── config/
│       │   ├── config.json
│       │   └── llm_config.json
│       ├── specpatchguard/
│       │   ├── core/          # Step 1: config_loader.py, llm_config.py, doctor.py
│       │   ├── data/          # Step 2: pkl_loader.py, d4j_fallback.py, patch_blind.py, tier_classifier.py, schemas.py
│       │   ├── sandbox/       # Step 3: docker_backend.py, d4j_runner.py, junit_injector.py
│       │   ├── analysis/      # Step 4: Docker 内 LSP / 可配 context grep / 分支与类属性工具
│       │   ├── prompts/       # Step 5: Intent / Cross-Rep / PBT 提示词模板
│       │   ├── strategies/    # Step 5: Tier-A Python+Hypothesis & Standalone JUnit (预留 Tier-B 占位)
│       │   ├── agents/        # Step 5+: llm_client.py, intent_agent.py, pbt_agent.py
│       │   └── ...            # 后续 Step 6-7 按分层子包扩展
│       └── tests/
│           ├── core/
│           ├── data/
│           ├── sandbox/
│           ├── analysis/
│           ├── strategies/
│           └── agents/
├── Patch_Va/
│   └── furina_context_data/   # 预处理好的 .pkl / bug_report / manifest
└── benchmark/
    └── rule.md
```

> 若服务器上的相对位置与上述不同，请务必在 `SpecPatchGuard/config/config.json` 中更新 `data.*` 路径。

---

## 2. 创建并填写 `.env`（API 密钥与端点）

在 `SpecPatchGuard/` 根目录（或 `SpecPatchGuard/config/` 目录）下复制模板创建 `.env`（该文件已被 `.gitignore` 忽略，严禁提交到 Git）：

```bash
cd /data/patch_va/myown/SpecPatchGuard
cp .env.example .env
```

按服务器实际可用的模型服务填写以下 3 个变量（或在系统环境变量中导出同名变量；**系统环境变量优先级高于 `.env`**；注意若系统环境中导出空字符串 `OPENAI_API_KEY=""`，也会屏蔽 `.env`，不用的空变量请 `unset`）：

| 变量名 | 说明 | 示例值 |
| :--- | :--- | :--- |
| `OPENAI_API_KEY` | 模型服务 API Key（必填，代码与日志永不回显） | `sk-...` |
| `OPENAI_API_BASE` | OpenAI 兼容服务 Base URL（必须为 `http`/`https`，不得内嵌账号密码） | `https://api.example.com/v1` |
| `LLM_MODEL` | 默认推理模型名 | `deepseek-chat` |

---

## 3. 修改 `SpecPatchGuard/config/config.json`（8 节逐项核对）

| 配置节 | 键名 | 默认值 | 上传服务器后需确认/修改的内容 |
| :--- | :--- | :--- | :--- |
| **`data`** | `patch_info_pkl` | `../../../Patch_Va/furina_context_data/d4j_custom_function_level_patch.pkl` | 确认服务器预处理候选补丁 `.pkl` 实际位置 |
| **`data`** | `test_info_pkl` | `../../../Patch_Va/furina_context_data/labeled_d4j20_enhance_info.pkl` | 确认服务器失败测试信息 `.pkl` 实际位置 |
| **`data`** | `bug_report_json` | `../../../Patch_Va/furina_context_data/clean_d4j20_bug_report.json` | 确认服务器 Bug Report JSON 实际位置 |
| **`data`** | `manifest_json` | `../../../Patch_Va/furina_context_data/manifest.json` | 确认预处理清单 `manifest.json` 实际位置 |
| **`data`** | `rule_md_path` | `../../../benchmark/rule.md` | 确认 `rule.md` 在服务器上的相对或绝对路径 |
| **`api`** | `base_url_env` / `api_key_env` / `model_env` | `OPENAI_API_BASE` / `OPENAI_API_KEY` / `LLM_MODEL` | 与 `.env` 或系统环境变量名保持一致 |
| **`api`** | `llm_config_path` | `./llm_config.json` | 通用 LLM 配置文件路径（定义多模型 profile、`context_window`、`max_input_tokens`、`max_tokens`/`max_completion_tokens`、`temperature`/`top_p`/`top_k`、思考模式开关、900s 超时与按 Agent 角色覆盖；优先于 `config.json -> api` 基础默认值） |
| **`api`** | `temperature` / `max_tokens` / `timeout_seconds` / `max_retries` | `0.0` / `8192` / `900` / `2` | 基础兜底超时（默认 900s）与重试参数；具体模型思考/上下文/token 限制在 `config/llm_config.json` 中切换 |
| **`docker`** | `image_name` | `d4j-lsp-runtime:latest` | 改为服务器上已构建好的 **Defects4J + jdtls** Docker 镜像标签 |
| **`docker`** | `host_mount_root` | `/data/patch_va/d4j_work` | 宿主机上存放 D4J checkout 工作区的真实绝对路径（须提前 `mkdir -p` 创建） |
| **`docker`** | `container_work_root` | `/workspace/d4j_work` | 容器内挂载工作区根目录（须为 `/` 开头的 POSIX 绝对路径，与 `host_mount_root` 映射对应） |
| **`docker`** | `timeout_seconds` / `nano_cpus` | `900` / `8000000000` | 单容器命令超时（默认 900s；容器内需具备 GNU `timeout` 以确保超时杀进程）与 CPU 配额（`8000000000` = 8 核） |
| **`defects4j`** | `d4j_home` | `/data/public/defects4j` | **容器内** Defects4J 根目录（`doctor` 会验证 `$d4j_home/framework/bin/defects4j` 可执行） |
| **`defects4j`** | `java8_home` | `/workspace/envs/defects4j` | **容器内** JDK 8 根目录（`doctor` 强校验 `java -version` 主版本为 `8`） |
| **`defects4j`** | `compile_timeout_sec` / `test_timeout_sec` | `900` / `900` | D4J 编译与 JUnit 执行超时（默认 900s） |
| **`lsp`** | `enabled` / `mode` | `true` / `docker_jdtls` | 可选 `docker_jdtls`（容器内运行）、`local_jdtls`（宿主机）或 `disabled` |
| **`lsp`** | `jdtls_home` | `/opt/jdtls` | Eclipse `jdtls` 安装目录（需与 D4J 处于同一容器或共享挂载同一 checkout） |
| **`lsp`** | `jdtls_java_home` | `/opt/jdk-17` | 运行 `jdtls` 的 **JDK 17+** 路径（必须与 `defects4j.java8_home` 分离，`doctor` 强校验 `>= 17`） |
| **`lsp`** | `jdtls_config_dir` | `/opt/jdtls/config_linux` | Linux 下对应 `config_linux` |
| **`lsp`** | `jdtls_workspace_cache` | `/tmp/jdtls_ws_cache` | `jdtls` `-data` 工作区索引缓存目录 |
| **`lsp`** | `init_timeout_sec` / `request_timeout_sec` | `900` / `900` | LSP 初始化与单次跳转查询超时（默认 900s） |
| **`tools`** | `grep_default_context_before` / `grep_default_context_after` / `grep_max_context_lines` / `grep_max_matches` | `5` / `5` / `50` / `30` | Docker 内 `grep_code` 默认前后上下文行数、最大上下文行数上限与最大匹配条数 |
| **`tools`** | `read_default_context_before` / `read_default_context_after` / `read_max_lines` / `branch_context_lines` | `10` / `10` / `200` / `3` | `read_file_lines` 默认前后行/单次最大行数，及条件分支提取附带扩展行数 |
| **`tools`** | `max_tool_calls_per_agent` / `max_tool_output_chars` / `command_timeout_sec` | `15` / `24000` / `900` | 单 Agent 工具调用预算上限、输出截断字符数与容器工具超时（默认 900s） |
| **`output`** | `result_dir` | `../SpecPatchGuard_result` | 最终评测与日志输出目录（不可与输入或配置文件路径冲突） |
| **`output`** | `spec_cache_dir` | `../SpecPatchGuard_result/frozen_specs` | 冻结规约与 `WitnessBank` 缓存目录 |
| **`output`** | `resume` | `true` | 是否开启断点续跑 |
| **`pipeline`** | `target_tier` / `enable_cross_rep_tier_a` | `tier_a` / `true` | 首批聚焦 Tier-A 简单函数 |
| **`pipeline`** | `enable_cross_rep_tier_b` | `false` | **Tier-B 预留开关**：二期扩展至 `Closure` 等重状态项目时设为 `true`（或 `target_tier` 改为 `tier_b`/`auto`） |
| **`pipeline`** | `enable_anchor_infection_probes` / `pbt_refine_max_rounds` / `syntax_fix_max_rounds` / `hypothesis_max_examples` | `true` / `5` / `3` / `100` | 探针、精化轮次与 Hypothesis 采样上限 |

---

## 4. 一键扫描代码内 `# [SERVER_CONFIG_REQUIRED]` 防遗忘标记

所有依赖服务器路径或外部环境的源码位置均已标注 `# [SERVER_CONFIG_REQUIRED]`，上传前/部署时可随时扫描确认：

- **Linux / macOS**：
  ```bash
  rg -n "SERVER_CONFIG_REQUIRED" specpatchguard/ .env.example
  ```
- **Windows PowerShell**：
  ```powershell
  Get-ChildItem -Path specpatchguard -Recurse -Filter *.py | Select-String -Pattern "SERVER_CONFIG_REQUIRED"
  ```

---

## 5. 上传服务器后的自检步骤（`check-config` & `doctor`）

在 `SpecPatchGuard/` 项目根目录执行：

1. **校验配置语法与 `.env` 读取（不碰大文件、不连网）**：
   ```bash
   python3 -m specpatchguard check-config
   ```
   - 退出码 `0`：配置结构合法。检查返回的 `pending` 列表应为空 `[]`。
   - 退出码 `2`：`config/config.json` 或 `.env` 存在格式/字段错误。

2. **运行服务器全量只读自检（含 Docker 镜像、宿主机挂载目录、D4J JDK 8、jdtls JDK 17+）**：
   ```bash
   python3 -m specpatchguard doctor --mode server
   ```
   如需同时验证 LLM API 端点连通性（发送一次轻量 `GET /models`，不消耗对话 token）：
   ```bash
   python3 -m specpatchguard doctor --mode server --check-api
   ```

3. **退出码含义**：
   - `0`：所有必检项通过（`overall_ok: true`），可以进入后续运行阶段。
   - `1`：配置可加载，但至少有一项前置条件失败（查看 JSON 中 `status == "fail"` 项的 `detail` 修复路径或镜像）。
   - `2`：配置文件本身加载或校验失败。

4. **Step 5 服务器 Python 依赖核对（Tier-A Cross-Rep）**：
   - 建议在服务器运行环境安装 `openai` 与 `hypothesis`：
     ```bash
     python3 -c "import openai, hypothesis; print('openai & hypothesis OK')"
     ```
   - 若服务器暂未安装 `hypothesis`，`TierAPythonHypothesisStrategy` 会自动降级为纯 Python 标准库边界采样并在 `diagnostics["hypothesis_available"]` 中记录。
