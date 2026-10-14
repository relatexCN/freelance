# Freelance 官网 / Website

项目官网采用暖白背景、深色文字、橙色主按钮、细线边框和代码展示，参考 [DESIGN.md](DESIGN.md)，保留 Freelance 自身品牌与实际功能。中文与英文为独立静态页面，无需网关后端、前端框架或 npm 依赖。

## 构建与预览 / Build and preview

需要 Node.js 18+。在项目根目录运行：

```powershell
node website/scripts/build.mjs
python -m http.server 8080 --bind 127.0.0.1 --directory website/dist
```

- 中文：`http://127.0.0.1:8080/`
- English: `http://127.0.0.1:8080/en/`
- 导航栏与页脚提供语言切换，切换后保留当前章节锚点。
- `Ctrl+C` 停止预览服务。仅将 `website/dist` 作为静态服务目录，不需要暴露项目配置文件。

**English:** Run the commands above from the repository root. The first command builds both languages; the second serves the generated site locally. No package installation is required. Stop the preview with `Ctrl+C`.

## 页面与交互 / Content and interactions

- 项目介绍、OpenAI 兼容接口、多供应商接入、回退与冷却、多模型 HTTP/MCP。
- 模型优先级演示：上下移动候选，模拟首选失败，切换默认排序与示例排序，复制 YAML 配置。
- 桌面操作示意：启动、停止与重启，仅在浏览器内改变演示状态。
- 快速开始：Windows 便携版构建、服务端 CLI 和 Python API 三个入口；可复制命令。
- FAQ、项目仓库与文档链接、移动端导航、键盘操作、减少动态效果偏好。

所有网关状态与模型名称均为示例，不发送 API 请求、不访问本地网关、不收集 Key。网站未提供下载托管；“构建 Windows 便携版”引导至现有构建命令。MIT 指项目许可，上游价格与配额由供应商决定。

**English:** The site introduces the real gateway features and offers browser-local routing and desktop illustrations. All demo data is illustrative. No API calls or key collection occur. The Windows CTA leads to build instructions rather than an unhosted download.

## 维护 / Maintenance

```text
website/
  DESIGN.md                原始设计参考 / Original design reference
  README.md                构建与部署说明 / Build and deployment instructions
  package.json             可选 npm 命令 / Optional npm scripts
  scripts/build.mjs        无依赖静态构建 / Dependency-free static build
  scripts/check.mjs        构建产物检查 / Static output checks
  src/content.mjs          中英文文案 / Chinese and English copy
  src/template.html        共享页面结构 / Shared page template
  src/site.css             设计与响应式样式 / Design and responsive styles
  src/site.js              本地交互 / Browser-local interactions
  src/favicon.svg          Freelance 标记 / Freelance mark
  dist/                    可部署产物 / Deployable output (generated, Git ignored)
```

修改 `src` 后重新构建。字体使用本机 sans-serif 和 monospace 回退，不加载授权字体或外部字体服务。参考文档中的 CursorGothic 不随项目分发。

```powershell
node --check website/src/site.js
node website/scripts/check.mjs
```

检查脚本验证双语元数据、静态资源、内部链接、外链安全属性与残留模板标记；不访问真实模型服务或验证外部文档是否公开。仓库文档链接目前指向 Gitee 的 `desktop` 分支，仓库地址或公开分支变化时修改 `src/template.html`。

**English:** Edit `src` and rebuild. Copy is in `content.mjs`, shared layout in `template.html`, styles in `site.css`, and interactions in `site.js`. System font fallbacks keep the site self-contained. Run the checks above after changes.

## 部署 / Deployment

### 引用生成的友链片段 / Referencing generated friend links

官网首页只包含友链容器及 `data-friends-src` 引用，不直接写入友链列表。浏览器读取通用页脚项目生成的 `friends-static.html`，仅提取友链名称和安全的 HTTP/HTTPS 链接，用官网自身样式显示。中英文栏目标题自动适配，不执行片段中的脚本或复制完整页脚。

默认构建读取相邻项目 `common_footer/friends-static.html`，将生成片段作为独立文件放入 `dist/assets/friends-static.html`；不存在相邻项目时使用 `src/friends-static.html` 中的离线片段。无需手动复制友链到页面。也可指定生成文件的位置：

```powershell
$env:FRIENDS_HTML_PATH = 'D:/www/common_footer/friends-static.html'
node website/scripts/build.mjs
```

如果已将通用项目的生成片段发布到共享网址，可让官网直接引用它：

```powershell
$env:FRIENDS_HTML_URL = 'https://your-footer-host.example/friends-static.html'
node website/scripts/build.mjs
```

上述网址是占位示例，请替换为实际发布地址。共享网址更新生成片段后，官网刷新即可获取最新友链，不需重新构建官网；共享服务器需允许官网来源的 CORS，HTTPS 官网应使用 HTTPS 片段。默认随包片段更新时，替换 `dist/assets/friends-static.html` 即可，无需改动中英文首页。普通浏览器不能读取 `D:/...` 文件路径，需要通过 HTTP 服务提供片段。加载失败或片段为空时隐藏栏目，其他页面功能正常。

使用共享网址后，如需恢复默认随包片段，清除环境变量再构建：

```powershell
Remove-Item Env:FRIENDS_HTML_URL -ErrorAction SilentlyContinue
node website/scripts/build.mjs
```

**English:** The page references a generated HTML fragment rather than inlining its links. By default the build publishes the common footer fragment as `assets/friends-static.html`. Set `FRIENDS_HTML_URL` to a hosted shared fragment to reuse it across websites without rebuilding each site's pages. Cross-origin hosting requires CORS. The loader extracts safe link data only, localizes the heading, and hides the section on failure.

仅发布 `website/dist` 内的内容，可部署到 Nginx、静态托管平台或对象存储。首页为 `index.html`，英文入口为 `en/index.html`；相对资源路径同时支持域名根目录和子路径。无需 SPA 路由回退。请勿发布项目根目录、`.env`、网关配置或业务日志。

**English:** Deploy only the contents of `website/dist` to a static host. Both languages use ordinary HTML pages with relative asset paths and need no server-side rendering or SPA fallback. Hosting configuration and a production domain are not created by the local build.

## 设计参考 / Design reference

原始参考：Cursor Inspired Design System Analysis。样式来源说明及预览：<https://getdesign.md/cursor/design-md>。此链接是设计参考，官网并非 Cursor 产品，也不使用其授权字体、标志、用户案例或产品声明。
