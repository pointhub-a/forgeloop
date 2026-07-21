# ForgeLoop

## 项目简介

ForgeLoop 是一个可测试、可审计的 Coding Agent Harness。模型每次只提出一个结构化动作；项目自己实现的循环负责策略判定、工作区工具分发、测试反馈、无进展检测、记忆和人工审批。主要工程贡献是确定性反馈闭环：测试失败会被分类、生成指纹并回灌，只有最新验证通过后任务才能成功。

无需 API Key 即可运行 `ScriptedProvider` 演示。真实模式包括使用严格 `json_schema` 的 `openai`，以及面向 New API 网关、使用 `json_object` 加本地严格 Action 校验的 `newapi`。OpenAI 示例默认 `gpt-5.6-luna`；选择依据是 [OpenAI 官方模型目录](https://developers.openai.com/api/docs/models) 与 [GPT-5.6 Luna 官方模型页](https://developers.openai.com/api/docs/models/gpt-5.6-luna)。模型和 HTTPS 端点均可在 TOML 中覆盖。

## 安装与运行

要求 Python 3.12。下面是在全新 POSIX 机器上从仓库根目录开始的原生冷启动命令：

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e ".[dev]"
make test
python3 scripts/mechanism_demo.py --json
forgeloop serve --provider demo --data-dir .forgeloop
```

打开 <http://127.0.0.1:8000>。`demo` Provider 不读取凭据；WebUI 可创建任务、查看事件/验证轨迹、处理一次性审批并运行固定机制演示。示例配置可通过 `--config forgeloop.example.toml` 加载；其中的验证器在每个任务所选工作区内执行。

Windows 支持 Python 3.12 原生运行，但需用 PowerShell 激活命令 `.venv\Scripts\Activate.ps1`，且 `make` 不是必要依赖：可直接运行 `.venv\Scripts\python.exe -m pytest -q`。Keyring 是否可用取决于系统凭据服务。

真实 Provider 是明确 opt-in。先通过隐藏输入保存凭据，再启动：

```bash
forgeloop credentials set openai
forgeloop credentials status openai
forgeloop serve --provider openai --config forgeloop.example.toml --data-dir .forgeloop
```

更新凭据时再次执行 `credentials set openai`；清除时执行 `forgeloop credentials clear openai`。状态只报告是否配置及来源，不回显内容。

### njusehub / New API

njusehub 使用独立的 `newapi` 凭据槽，不会读取或复制 `openai` Key：

```bash
source .venv/bin/activate
forgeloop credentials set newapi
forgeloop credentials status newapi
forgeloop serve --provider newapi \
  --config njusehub.example.toml \
  --data-dir .forgeloop
```

`njusehub.example.toml` 不含凭据，默认端点为 `https://njusehub.info/v1/chat/completions`、模型为 `qwen-turbo`、单次请求超时为 20 秒。`newapi` 会要求模型返回单一 JSON object，再由本地 `parse_action()`、策略和工具边界严格校验；不会执行上游 `tool_calls`。

一个 Agent 步骤遇到超时、连接错误、HTTP 429/5xx 或空内容时，最多发出两次 HTTP 请求，中间固定等待 250 ms；两次请求可能分别计费。HTTP 400/401/403、畸形响应和非空但非法的 Action 不做传输重试。修改 Key、端点或模型后必须重启服务并新建任务，旧的活跃任务不会迁移。

如果出现 `zsh: command not found: forgeloop`，先在仓库根目录执行 `source .venv/bin/activate`；也可以直接使用 `.venv/bin/forgeloop`。如果 8000 端口已被占用，改用 `--port 8001`，再访问 <http://127.0.0.1:8001>。

## 分发

本仓库同时支持 Python wheel/sdist 和 Linux 容器。构建本地包：

```bash
python3 -m build
python3 -m pip install dist/forgeloop-0.1.0-py3-none-any.whl
forgeloop demo --json
```

Docker 默认启动无 Key 的 demo WebUI，并只把宿主回环地址发布到容器：

```bash
docker build -t forgeloop:local .
docker run --rm --name forgeloop \
  -p 127.0.0.1:8000:8000 \
  --mount type=volume,src=forgeloop-workspace,dst=/workspace \
  --mount type=volume,src=forgeloop-data,dst=/data \
  forgeloop:local
```

Compose 冷启动等价命令：

```bash
docker compose up --build
```

健康检查地址为 <http://127.0.0.1:8000/healthz>。镜像使用多阶段 `python:3.12-slim` 构建，只安装 wheel，最终以 UID 10001 的非 root `forgeloop` 用户运行；源包层为只读。GitLab 流水线先执行 `unit-test`，通过后执行 `container-build`。镜像 registry 与公开 WebUI URL 需要仓库所有者在其 GitLab/容器平台上配置和发布，本仓库不伪造外部执行记录。

真实容器模式必须挂载 owner-only 文件，并让进程以该文件所有者 UID/GID 运行。下列命令不会把 Key 写入命令参数、镜像或环境变量；`FORGELOOP_SECRET_FILE` 的值只是容器内路径：

```bash
mkdir -p "$HOME/.config/forgeloop" "$PWD/.forgeloop-data" "$PWD/workspace"
chmod 700 "$HOME/.config/forgeloop" "$PWD/.forgeloop-data" "$PWD/workspace"
secret_path="$HOME/.config/forgeloop/openai-key"
SECRET_PATH="$secret_path" python3 - <<'PY'
import getpass
import os
from pathlib import Path

secret_path = Path(os.environ["SECRET_PATH"])
temporary_path = secret_path.with_name(
    f".{secret_path.name}.{os.getpid()}.tmp"
)
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
flags |= getattr(os, "O_CLOEXEC", 0)
flags |= getattr(os, "O_NOFOLLOW", 0)
secret = getpass.getpass("OpenAI API key: ")
if not secret.strip():
    raise SystemExit("Credential must not be empty.")

descriptor = os.open(temporary_path, flags, 0o600)
try:
    os.fchmod(descriptor, 0o600)
    secret_file = os.fdopen(descriptor, "w", encoding="utf-8")
    descriptor = -1
    with secret_file:
        secret_file.write(secret)
    os.replace(temporary_path, secret_path)
except BaseException:
    if descriptor >= 0:
        os.close(descriptor)
    try:
        temporary_path.unlink()
    except FileNotFoundError:
        pass
    raise
finally:
    secret = ""
PY
docker run --rm --name forgeloop-openai \
  --user "$(id -u):$(id -g)" \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  -p 127.0.0.1:8000:8000 \
  --mount type=bind,src="$PWD/workspace",dst=/workspace \
  --mount type=bind,src="$PWD/.forgeloop-data",dst=/data \
  --mount type=bind,src="$secret_path",dst=/run/secrets/forgeloop_api_key,readonly \
  --env FORGELOOP_SECRET_FILE=/run/secrets/forgeloop_api_key \
  forgeloop:local \
  forgeloop serve --provider openai --host 0.0.0.0 --port 8000 \
    --allow-remote --allowed-host localhost --allowed-host 127.0.0.1 \
    --data-dir /data
```

以上 Python `getpass` heredoc 在 Bash/Zsh 下都从 TTY 隐藏读取，Key 不进入命令参数、shell history 或环境变量；环境中的 `SECRET_PATH` 仍只是一条路径。Linux Docker Engine 可直接使用上述 UID 映射；Docker Desktop 的文件共享层也必须允许这些路径。要轮换文件凭据，停止服务，用同样流程原子替换该 `0600` 文件，再重启；secret-file 后端本身是只读的，`credentials set/clear` 会拒绝修改。

## 目录结构

```text
src/forgeloop/       自写 Agent 循环、策略、工具、反馈、记忆、持久层、Web/CLI
tests/               离线单元与集成测试；ScriptedProvider 替代真实 LLM
scripts/             可重复的机制演示
forgeloop.example.toml  完整、无凭据的示例配置
njusehub.example.toml   njusehub/New API 的无凭据示例配置
Dockerfile           wheel 多阶段 OCI 构建
compose.yaml         无凭据 demo 冷启动
.gitlab-ci.yml       unit-test 与 container-build jobs
SPEC.md / PLAN.md    已批准规约与实现计划
SPEC_PROCESS.md      规约冷启动验证证据
AGENT_LOG.md         TDD、评审和提交过程证据
REFLECTION.md        仅由学生本人完成的反思工作表
```

核心依赖方向是 `Web/CLI → TaskService → AgentLoop → Provider/Policy/Tool/Feedback/Memory`；SQLite、OpenAI 和 New API HTTP 适配器位于外层，Provider 永远拿不到工具对象。

## 凭据安全

原生模式默认使用操作系统 Keyring：macOS Keychain、Windows Credential Manager，或已配置 Secret Service 的 Linux 桌面会话。首次录入和更新使用 `getpass` 隐藏输入；`status` 不显示 Key；`clear` 从 Keyring 删除。Web 设置页提供同一生命周期，但不会把值渲染回 HTML。

容器模式设置 `FORGELOOP_SECRET_FILE` 后，CLI 会把 `SecretFileBackend` 绑定到当前显式选择的 Provider（例如 `openai` 或 `newapi`）。后端拒绝符号链接、非普通文件和任何 group/other 权限位，每次读取都会重新校验；内容只在 CredentialService 内判定状态或由 Provider composition root 取得，Web/状态输出均不回显。该环境变量必须是路径，绝不能放 Key 本身。

项目不自动加载 `.env`。`.env` 已被 Git 和 Docker context 排除，但它仍是明文文件，且导出后会出现在进程环境及同权限诊断接口中；不要把它当作推荐存储，也不要在 shell 命令行中直接写 Key。事件、SQLite、Web 页面和错误输出有结构化边界及令牌脱敏；提交前仍应运行仓库 secret 审计。

## 安全边界

ForgeLoop 提供这些代码级保证：工作区路径在解析符号链接后仍须留在根目录；命令使用 argv 与 `shell=False`；可执行程序有 allowlist；危险规则要求动作绑定的一次性审批；输出、文件、子进程时间和步骤有界；API Key 不进入工具子进程环境；成功状态需要最新验证通过；Web 拒绝未列入 allowed-host 的 Host 头。

这些机制不是完整沙箱。allowlist 中的解释器或测试程序仍可能执行任意项目代码，人工批准也不能证明动作安全；容器、独立低权限账户和最小挂载面仍是生产隔离要求。WebUI 没有多用户认证或 TLS，默认只应通过 `127.0.0.1` 使用，不应直接暴露公网。

容器的 `0.0.0.0` 是监听地址，不是可信 HTTP Host。默认命令明确允许 `localhost` 与 `127.0.0.1`。反向代理或自定义域名部署必须为每个实际 Host 增加一个具体的 `--allowed-host example.invalid`，同时保留 `--allow-remote`；通配 Host 会被拒绝。代理层还必须自行提供认证、TLS 和来源控制。

## 已知限制

- 活跃 AgentLoop/Provider 上下文只在当前服务进程内。服务重启后，任务、事件和审批审计仍可读取，但此前活跃任务不能继续推进，会返回 `TaskNotLoaded`/HTTP 409；请创建新任务，不要把重启当作 live-loop resume。
- `newapi` 只承诺 New API 的 OpenAI-compatible Chat Completions + `json_object` 路径；官方 DeepSeek、阿里云 DashScope 等原生协议尚未单独适配，也不做自动模型切换。
- 容器镜像包含 ForgeLoop 运行依赖，不包含任意目标项目的编译器、pytest 插件或系统库。验证器必须在 ForgeLoop 所在环境可执行；复杂项目应构建派生镜像。
- Linux 原生 Keyring 需要可用的 Secret Service/DBus 会话；无桌面服务时使用 owner-only secret-file 容器路径，而不是明文降级。
- 首版一次只在一个进程内串行推进单个任务，不提供多 Agent、分布式队列、向量检索或主动恢复。
- SQLite 适合单机；不支持多副本共享写入。WebUI 没有公网部署所需的认证、TLS、限流和租户隔离。
- 官方 `python:3.12-slim` 提供 amd64/arm64 等平台镜像；实际目标项目工具链和本地 Keyring 后端仍受宿主平台限制。Windows 原生路径尚未做与 POSIX 等量的端到端容器验证。
- 公开 registry、最终 GitLab CI pass 记录和线上 URL 需要所有者账户权限，必须在提交前由所有者实际推送/部署并记录，不能由本地测试替代。

常见故障：`credential backend is unavailable` 表示系统 Keyring/Secret Service 不可用；`secret file must not grant group or other permissions` 需执行 `chmod 600` 并确认进程 UID 是文件所有者；Provider HTTP 401 表示当前模式的凭据无效或未被网关接受；HTTP 403 常由 Host 未加入 `--allowed-host` 引起；wildcard bind 错误需同时给出 `--allow-remote` 和至少一个具体 allowed host；容器写入失败需确认 `/workspace`、`/data` 的宿主目录归当前映射 UID 所有。

## 第三方许可证

仓库未复制第三方源码。直接运行依赖为 [FastAPI](https://github.com/fastapi/fastapi)（MIT）、[Jinja2](https://github.com/pallets/jinja/)（BSD-3-Clause）、[keyring](https://github.com/jaraco/keyring)（MIT）、[Pydantic](https://github.com/pydantic/pydantic)（MIT）和 [Uvicorn](https://github.com/Kludex/uvicorn)（BSD-3-Clause）。开发/测试直接依赖为 [build](https://github.com/pypa/build)（MIT）、[httpx2](https://github.com/pydantic/httpx2)（BSD-3-Clause）和 [pytest](https://github.com/pytest-dev/pytest)（MIT）。容器基于 [Docker Official Image `python`](https://hub.docker.com/_/python)；其组件许可证以对应镜像标签随附材料为准。使用和再分发时应同时遵守这些上游许可证及其传递依赖许可证。
