# 私有高质量全局语音输入（FunASR + Windows）

这套方案把「录音 -> GPU FunASR -> CPU fallback -> 当前输入框」串起来。Windows 客户端只需要一个快捷键，Codex、Terminal、Joplin、浏览器、VS Code、ChatGPT 等所有可粘贴文本的程序都能使用。

## 1. 本机网关（CPU fallback）

```bash
cd ~/ASR
cp .env.example .env
openssl rand -hex 32                 # 将输出填入 .env 的 ASR_API_TOKEN
docker compose up -d --build
curl http://127.0.0.1:45679/v1/health
```

健康检查中的 `ok=true` 表示 HTTP 服务正常；`ready=true` 表示模型已经加载。首次模型下载前 `ready` 可能是 `false`，这是正常的。

如果 Docker Hub 在当前网络不可达，可执行 `bash deploy/build-with-mirror.sh` 使用镜像代理拉取基础镜像，再执行 `docker compose up -d`。脚本只处理基础镜像，Python 包仍从 PyPI 下载。

若 Docker/PyPI/ModelScope 网速很慢，`.env` 中的 `ASR_PROXY` 会同时用于镜像构建和容器运行。Linux Docker 通过 `host.docker.internal:host-gateway` 访问宿主机代理；默认示例为 `http://host.docker.internal:7890`。代理程序必须监听宿主机网卡（不能只监听 `127.0.0.1`），并允许局域网客户端访问。修改后执行 `docker compose build --no-cache && docker compose up -d`；模型会缓存到 `models` volume。

网关默认只绑定 `127.0.0.1:45679`，推荐由 Nginx/Caddy 提供公网 HTTPS。若端口已被占用，在 `.env` 设置其他高位 `ASR_PORT`，并让反代指向对应端口。若暂时不用反代、需要直接通过公网 IP 访问，显式设置 `ASR_BIND=0.0.0.0`，同时用防火墙限制来源。

首次识别会下载模型并缓存于 Docker volume。默认语言是 `zh`，适合中文为主、夹杂 English/代码术语的口语；识别结果会统一转换为简体中文，并默认保守移除明显的重复填充音（如“呃呃”“嗯嗯”），英文、数字和代码标识符会保留。CPU fallback 默认使用 `faster-whisper`（`small`），避免在无 GPU 的中转机上等待 FunASR 的额外模型源；GPU 服务默认使用 FunASR `SenseVoiceSmall`。CPU 镜像会从 PyTorch CPU wheel 源明确安装 PyTorch；GPU 镜像安装 CUDA 版 PyTorch，不会被 CPU wheel 覆盖。Windows 客户端发送 WAV，因此默认镜像不安装体积很大的 FFmpeg；若要直接上传 MP3/M4A，请自行在 Dockerfile 的 apt 安装行加入 `ffmpeg`。如需本机也使用 FunASR，可在 `.env` 设置 `ASR_ENGINE=funasr`；`auto` 仍会在 FunASR 初始化失败时切换 faster-whisper。

`ASR_INFERENCE_TIMEOUT` 控制单次模型初始化/推理的最长秒数，默认 300；模型源不可达时返回 503，而不会阻塞整个 HTTP 服务。

`ASR_GPU_URL` 填 GPU 服务器的内网地址，例如 `http://10.66.0.2:45680`。网关会强制绕过 HTTP 代理直接访问该私网地址。GPU 不可达时网关自动在本机 CPU 推理；传 `no_fallback=true` 会在 GPU 失败时直接返回 502。GPU Compose 默认只绑定 `127.0.0.1`；部署时在 GPU 机的 `.env` 将 `ASR_GPU_BIND` 设置为其局域网或 WireGuard IP，并用主机防火墙限制来源。

## 2. NVIDIA GPU 服务器

GPU 机安装 NVIDIA 驱动、Docker 和 `nvidia-container-toolkit`，确认 `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi` 成功。将 `gpu-server/.env.example` 复制为 `gpu-server/.env`，设置与网关**相同**的 token：

```bash
cd ~/ASR/gpu-server
cp .env.example .env
docker compose up -d --build
curl http://127.0.0.1:8080/v1/health
```

可通过可信局域网或 WireGuard 直连 GPU 服务，不需要额外反向代理；不要把 GPU 端口映射到公网。在 GPU 机的 `.env` 设置 `ASR_GPU_BIND=<GPU_PRIVATE_IP>` 和高位 `ASR_GPU_PORT`，在网关 `.env` 设置 `ASR_GPU_URL=http://<GPU_PRIVATE_IP>:<GPU_PORT>`。多 GPU 机器用从 0 开始的 `ASR_GPU_DEVICE_ID` 固定一张 GPU，避免 Compose 自动选择到正在运行其他任务的设备。

## 3. WireGuard 边界

使用 WireGuard 时，在 GPU 机防火墙只允许网关 WireGuard 地址访问 GPU 服务端口；公网只开放 WireGuard UDP 端口（通常 51820）。使用可信局域网直连时，应只绑定内网 IP，并按需限制网关来源。不要把 ASR token 放在 URL 中。生产环境可在网关前加 Caddy/Nginx HTTPS 和限流。

Windows 客户端的 `ASR_URL` 应设置为实际网关的 HTTPS 地址，例如 `https://asr.example.com`。使用 Caddy 时可从 `deploy/Caddyfile.example` 开始配置；使用 Nginx 时将反向代理上游指向 `.env` 中 `ASR_BIND:ASR_PORT` 对应的地址。

## 4. Windows 全局输入

安装 Python 3.11，安装客户端：

```powershell
cd $env:USERPROFILE\ASR\windows-client
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

`.env` 只用于首次启动默认值：设置 `ASR_URL=https://asr.example.com`，`ASR_TOKEN` 与服务一致。首次模型下载期间可保持 `ASR_REQUEST_TIMEOUT=900`，正常使用后可改成 180。随后：

```powershell
python asr_client.py
```

启动后会显示设置窗口，可修改服务地址、Token、语言、热词和请求超时。勾选或取消“启用二次润色”会立即保存，并作用于下一次请求；其他字段点击“保存设置”后生效。配置默认保存在 `%LOCALAPPDATA%\\ASR\\config.json`，`.env` 不会覆盖已经保存的 GUI 配置。

先点击要输入文字的目标输入框，再按 `Ctrl+Alt+Space` 开始录音，再按一次结束；也可以使用窗口中的录音按钮。识别结果会通过剪贴板 `Ctrl+V` 粘贴到仍然获得焦点的输入框。`Ctrl+Alt+P` 可在不打开窗口的情况下切换二次润色，并同步更新勾选状态和本地配置。正在处理的请求继续使用发出时的设置，修改后的状态从下一次请求生效。关闭窗口会退出客户端。首次使用需在 Windows 隐私设置允许麦克风；若目标程序以管理员身份运行，请也以管理员身份运行客户端（Windows 的全局键盘钩子限制）。

客户端会把每次录音、请求成功、HTTP 错误、网络错误和注入异常写入 `%LOCALAPPDATA%\\ASR\\asr-client.log`（JSON Lines，不记录 token 和音频）。成功记录中的 `route` 为 `gpu` 或 `cpu`；GPU 不通后回退时会有 `fallback_reason`。服务端超时会记录 HTTP `503`，GPU 强制失败（`no_fallback=true`）会记录 HTTP `502`。

要开机自动运行，在激活虚拟环境并安装依赖后执行：

```powershell
.\install-startup.ps1
```

它会在当前用户的 Startup 文件夹创建快捷方式；删除该快捷方式即可停用。

## API

`POST /v1/transcribe`，multipart 字段 `file`，请求头 `X-ASR-Token`，可选 query `language=zh|en|auto`、`hotwords=...`、`correct=true|false`。返回 `{"text":"...", "route":"gpu|cpu", "engine":"funasr", "correction":"applied|disabled|failed|unconfigured|skipped"}`。

需要自动标点、术语修正或长文本整体润色时，可在网关 `.env` 设置 `ASR_CORRECTOR_URL`、`ASR_CORRECTOR_MODEL` 和可选的 `ASR_CORRECTOR_KEY`。`ASR_CORRECTOR_PROMPT` 可自定义二次润色的 system prompt；缺失或留空时使用 `.env.example` 中的默认提示词。它调用兼容 OpenAI Chat Completions 的服务；`ASR_CORRECTOR_TIMEOUT` 控制润色调用的最长等待秒数，默认 60。本地模型地址若同时解析到不可用的 IPv6，可设置 `ASR_CORRECTOR_FORCE_IPV4=true`。留空或请求传 `correct=false` 时，仍会执行本地填充词清理、空白规范化和繁转简，但不会进行语义改写。`ASR_REMOVE_FILLERS=false` 可关闭本地填充词清理。

## 排查

```bash
docker compose logs -f asr
curl http://127.0.0.1:45679/v1/health
```

若模型下载失败，先在可联网环境启动一次并保留 `models` volume；若 GPU route 失败，检查 `curl http://<GPU_PRIVATE_IP>:<GPU_PORT>/v1/health`、路由和防火墙，使用 WireGuard 时还需检查 AllowedIPs。网关会自动回落 CPU。
