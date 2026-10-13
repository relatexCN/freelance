# 桌面客户端

桌面客户端用于本机管理网关，也能连接远程网关。API、CLI 和 MCP 无需安装桌面依赖即可继续使用。

## 安装和启动

使用 Python 3.11 或更新的受支持版本，在项目目录运行：

```powershell
python -m pip install -e ".[desktop]"
python -m app.desktop
```

也可以使用 `freelance-desktop` 命令，或 Windows 双击 `freelance-desktop.bat`。批处理优先启动 `dist/FreelanceDesktop/FreelanceDesktop.exe` 或解压后的 `FreelanceDesktop/FreelanceDesktop.exe`，此时无需安装 Python。没有便携版时，使用项目 `.venv`，否则使用 PATH 中的 Python；依赖缺失时显示所选解释器及对应安装命令。开发时直接运行 `python -m app.desktop` 使用源码。

默认本地配置目录为用户目录下的 `.freelance`，独立于仓库配置。第一次打开时创建最小供应商模板，并生成不同的调用 Key 和管理员 Key。已有 Key 不会被自动轮换，已有空值也不会被擅自替换。

使用已有部署配置：

```powershell
python -m app.desktop --workspace D:\www\freelance
```

配置目录可以在界面中选择，但当前目录的本地网关运行期间不能切换。界面显示的本地目录对应实际读写文件，不使用后端的工作目录回退逻辑。

## 本地使用

1. 在“供应商配置”选择模板或已有供应商，填写 Base URL、供应商 Key 和默认模型。
2. 保存配置。Key 输入留空表示保留，勾选“清空”表示删除该供应商的有效凭据；多个 Key 用英文逗号分隔。
3. 点击“启动本地网关”。网关只监听 `127.0.0.1`，启动中通过管理员状态接口显示状态。
4. 点击“连接 / 刷新”读取运行概览、模型及别名，使用“调用测试”验证单模型或多模型调用。
5. “接入指引”提供 OpenAI 兼容 Base URL 和示例，其他客户端使用调用 Key。

配置保存后重启生效，不执行热更新。修改供应商时保留已有高级字段；界面不编辑工具模式、评分、透传和自动排名等高级配置，可继续手工维护 YAML。

桌面客户端启动的是独立后端子进程，不使用 CLI 的 `freelance.pid`。停止和重启仅针对 PID、创建时间、命令行和配置目录均匹配的桌面进程。CLI 已启动的服务不会被桌面停止；端口冲突时更换端口，或用远程模式连接已有服务。

关闭窗口可选择“保留服务并退出”或“停止服务并退出”。再次以相同配置目录打开客户端可找回后台进程及其端口。Windows 停止使用进程终止，正在执行的请求可能被中断；不会保证上游立即停止计费。

## 远程使用

切换“远程网关”，填写服务地址与调用 Key。可填写 `http(s)://host:port` 或以 `/v1` 结尾的地址；反向代理路径前缀也支持，需同时代理 `/admin/*` 和业务接口。

基础模型列表、供应商状态和调用测试使用现有调用 Key。配置管理和统计快照另需服务端 `.env` 配置：

```dotenv
ADMIN_API_KEY=<独立的随机管理员密钥>
```

重启服务后，在桌面客户端填写这个管理员 Key。未设置时管理 API 返回 503；普通调用 Key 不提供管理权限。无需启用 `manager_agent`。远程地址和 Key 只在窗口内存中使用，不写入连接配置。

远程配置保存后需在服务器通过原有 CLI 或部署管理器重启。客户端不提供远程进程启停。推荐经 HTTPS 或受保护的网络连接，明文 HTTP 会明文传送 Key。

## 管理 API

均使用 `Authorization: Bearer <ADMIN_API_KEY>`：

| 接口 | 行为 |
| --- | --- |
| `GET /admin/status` | 启动中、就绪、失败或停止状态；HTTP 监听后即可访问 |
| `GET /admin/snapshot` | 脱敏的供应商、模型及当前进程累计统计 |
| `GET /admin/config` | 可编辑字段及配置版本，不返回供应商 Key 或敏感透传字段 |
| `PUT /admin/providers` | `{revision, provider: {name, base_url, default_model, timeout, api_key?}}` |
| `PUT /admin/aliases` | `{revision, route: {alias, provider, model}}` |
| `DELETE /admin/aliases` | `{revision, alias}` |

更新带上最近读取的 `revision`。文件变化或写入锁冲突返回 409，需要重新读取；非法字段返回 422，不回显包含 Key 的请求值。其他文件或输入错误返回通用 400。状态接口之外的管理接口在初始化期间仍返回 503，与原有业务就绪行为一致。

## 保存与数据边界

- 新填入的供应商 Key 写入 `.env` 的专用变量，YAML 使用变量引用；未修改 Key 时保留原有引用或字面值。
- 外部进程环境变量优先于 `.env`。若 Key 来自外部环境，界面禁止替换，需在部署环境修改；避免出现“保存成功却不生效”。
- 保存采用跨进程写锁、版本检查和单文件原子替换，保留一份 `config.yaml.bak` 和必要的 `.env.bak`。文件写入失败时尝试回滚 `.env`。两个文件不具备断电级事务保证；崩溃恢复需检查备份。
- 保存会重新序列化 YAML，保留配置数据及高级字段，但不保留注释和原有排版。备份可能含历史凭据，勿上传或公开；Windows 文件权限继承配置目录 ACL。
- 统计是进程内累计值，重启清零；流式成功计数可能只代表收到首包。没有历史趋势、费用或剩余额度。
- 单模型测试识别 SSE 中的业务错误，不能只把 HTTP 200 当成功。取消会关闭客户端接收连接，但不保证上游推理或计费立即停止。
- 多模型测试直接显示独立结果 JSON，不汇总、不执行工具；单模型工具指令也不会由测试界面执行。
- 本地启动日志展示最近约 32 KiB，并遮蔽当前 `.env` 中已知的长值；这不是对任意自由文本秘密的完整识别。

## Windows 便携版构建

```powershell
python -m pip install -e ".[desktop,desktop-build]"
python scripts/build_desktop.py
```

产物为 `dist/FreelanceDesktop/FreelanceDesktop.exe` 及同目录的运行依赖。将整个 `FreelanceDesktop` 文件夹分发或压缩，不能只复制 EXE。使用者无需安装 Python；配置仍保存在用户目录的 `.freelance`，构建不包含仓库 `.env`、运行日志和业务配置。

打包后本地模式仍使用同一个 EXE 以 `--backend` 参数启动独立后端，远程模式无需启动本地后端。当前未提供安装向导、代码签名或自动更新系统。

构建脚本在 Windows 下隔离第三方工具的 DLL 搜索路径，避免误打包其他工具的 ICU。可运行 `python scripts/verify_desktop_bundle.py`，在临时目录验证实际 EXE 的界面渲染、后端启动、API、进程找回和停止；不会请求真实上游。源码完整回归使用 `python -m unittest discover -s tests -q`，未安装 Qt 时界面测试跳过。

打包版启动异常记录在配置目录的 `desktop-error.log`；本地后端启动输出在 `desktop-launch.log`。界面预览可使用 `FreelanceDesktop.exe --workspace <临时目录> --preview <PNG路径>`，渲染后自动退出。
