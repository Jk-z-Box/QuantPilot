# Linux 安装、域名与 GitHub 发布

## 安装条件

推荐 Debian 12 / Ubuntu 24.04 或更新的 systemd 发行版，需要 Python 3.11–3.13、venv、pip 和网络。安装器不自动修改防火墙、DNS、Cloudflare 账户或系统 Python。

Debian/Ubuntu 可先安装依赖：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip ca-certificates git
```

其他发行版请使用相应包管理器提供 Python 3.11+ 和 systemd。

## 从交付压缩包安装

解压 `QuantPilot-0.3.0.zip`，进入 `QuantPilot` 目录，运行：

```bash
sudo bash scripts/install.sh --port 8080 --username admin
```

用户名、端口可以通过参数指定，也可以交互输入；密码只能在终端交互输入。首次安装拒绝覆盖已有配置。脚本创建无登录 shell 的 `quantpilot` 系统用户，安装固定且带哈希的依赖，创建 systemd 服务与快捷命令。

| 内容 | 服务器位置 |
|---|---|
| 每次安装的版本 | `/opt/quantpilot/releases/` |
| 当前版本指针 | `/opt/quantpilot/current` |
| 配置、账本、密钥 | `/var/lib/quantpilot/` |
| 可选 TLS 证书 | `/etc/quantpilot/origin.crt` |
| 可选 TLS 私钥 | `/etc/quantpilot/origin.key` |
| 管理入口 | `/usr/local/bin/qp`、`/usr/local/bin/quantpilot` |

默认监听 `127.0.0.1`，可以用 SSH 转发访问：

```bash
ssh -L 8080:127.0.0.1:8080 user@your-server
```

浏览器打开 `http://127.0.0.1:8080`。端口需要与 `public_origin` 完全一致，避免同源检查拒绝写入请求。

## Cloudflare Origin Rules

推荐路径：浏览器 HTTPS 443 → Cloudflare 橙云 DNS → Origin Rule 改目标端口 → 服务器该端口的 HTTPS 服务。端口覆盖不会把 HTTPS 自动转换为 HTTP。不要使用 Flexible 模式承载登录密码和交易所密钥。

1. 在 Cloudflare DNS 添加域名的 A/AAAA 记录，指向服务器，启用代理。
2. 准备覆盖该域名的 Origin CA 证书或受信任公共证书，私钥留在服务器。
3. 指定域名和证书安装 HTTPS 服务，例如：

```bash
sudo bash scripts/install.sh --port 8443 --username admin \
  --origin https://trade.example.com \
  --tls-cert /secure/path/origin.crt \
  --tls-key /secure/path/origin.key
```

此时服务监听 `0.0.0.0:8443`，用传入证书直接提供 HTTPS，Cookie 带 Secure 属性。服务器防火墙应只允许 Cloudflare 公布的源 IP 访问该回源端口；不要用不明来源的静态 IP 列表。

4. Cloudflare SSL/TLS 选择 **Full (strict)**，开启 HTTP→HTTPS 重定向。
5. Rules → Origin Rules 新增规则，匹配：`http.host eq "trade.example.com"`，将 Destination Port 重写为 `8443`。主机头、SNI 保持原值。
6. 对这个控制台域名配置缓存绕过；不要配置 Cache Everything。应用响应已经带 `Cache-Control: no-store`。
7. 从最终域名登录，检查刷新、保存策略、退出和新会话。用 Cloudflare Trace 确认端口规则命中。

生成单条规则 JSON：

```bash
qp cloudflare --domain trade.example.com --port 8443 > origin-rule.json
```

输出是**单条 rule**，供新增到现有 `http_request_origin` 阶段规则集；不是完整 ruleset，也不会调用 Cloudflare API。不要把它拿去覆盖整个现有规则集。域名与端口经过验证，避免把任意表达式拼进规则。

依据 [Cloudflare 官方端口覆盖示例](https://developers.cloudflare.com/rules/origin-rules/examples/change-port/) 和 [功能可用性](https://developers.cloudflare.com/rules/origin-rules/)，目标端口覆盖可用于各套餐；Host、DNS、SNI 覆盖有不同限制，本方案无需这些额外覆盖。

Origin CA 证书通常仅受 Cloudflare 信任，不能把浏览器直接访问源站的证书警告当作通过验证。最终验收使用 Cloudflare 域名；若需要直连验证，使用明确配置受信任 CA 与正确主机名的客户端。

也可由现有 Nginx/Caddy 提供 HTTPS，后端 QuantPilot 保持本机 HTTP。此时 `--origin` 填最终 HTTPS 域名，Origin Rule 指向代理的 TLS 监听端口；不要指向后端明文端口。应用不信任转发 IP 头，代理后登录限频会按代理 IP 聚合。

## 更新与回滚

从审阅后的新版本源码目录运行：

```bash
sudo bash scripts/install.sh --upgrade
```

更新保留配置与账本，先生成一致性备份，安装到新目录，再原子切换版本指针并重启。若 systemd/TCP 就绪检查失败，会恢复原版本指针。脚本就绪检查不是外部 HTTPS、Cloudflare 或业务验收；仍需执行登录及模拟下单验收。

0.2 保留旧表及 schema 标记，新增 v2 命名空间和订单表，无破坏性迁移。回滚旧代码后新账户数据保留，但旧界面不会展示。未来引入不兼容迁移前必须另行设计数据回滚，不能只切换代码。历史版本不会自动删除。

## 备份和恢复

`sudo qp backup` 使用 SQLite 在线备份 API，包括账本、配置和加密主密钥。文件权限为 0600，但它包含解密所需全部材料，必须按秘密备份保管。单独丢失 `master.key` 无法恢复凭据；服务拒绝悄悄生成替代密钥。

恢复时停止服务，先另存当前数据，再检查备份只含 `config.json`、`master.key`、`quantpilot.db`。在全新的恢复目录解包，确认权限/属主后更换数据目录内容，不要把旧 `quantpilot.db-wal` 或 `quantpilot.db-shm` 混入恢复结果。运行 `sudo qp doctor` 后启动。备份中的会话可能仍在有效期内，恢复后运行 `sudo qp password` 撤销会话。

## GitHub 仓库与远程安装

仓库：<https://github.com/Jk-z-Box/QuantPilot>，按用户要求公开。服务器下载不需要 GitHub 账号或 Token。

根目录 `install.sh` 是公开引导安装器，要求 `--ref` 指定经过验证的完整 40 位提交 ID。它从 GitHub HTTPS 下载对应源码，解压到私有临时目录，然后运行项目安装器；退出时清理临时文件。原始安装器仍交互设置端口、账号和密码，真实交易保持锁定。

发布说明会给出填入实际提交 ID 的完整一键命令。命令结构如下（将 `<COMMIT>` 替换为已验证的完整提交 ID）：

```bash
bash -o pipefail -c 'curl --proto "=https" --tlsv1.2 -fsSL https://raw.githubusercontent.com/Jk-z-Box/QuantPilot/<COMMIT>/install.sh | bash -s -- --ref <COMMIT>'
```

需要 Linux/systemd、Python 3.11+、venv、curl、tar 和 sudo/root。`pipefail` 确保引导脚本下载失败不会被误判为成功。可在内层命令的 `--ref <COMMIT>` 后追加 `--port 8080 --username admin` 或域名/TLS 参数。

从已安装的服务器更新时使用同一固定版本命令，并追加 `--upgrade`。不要直接跟随未经审查的可变分支安装。

公开仓库安装无需 Token。发布优先使用 GitHub 安全连接；任何临时发布凭据只能按明确授权在进程内存中使用，不写入部署命令。

### 启用 GitHub Actions

CI 保存在 `ci/github-actions.yml` 作为模板。通过具备相应权限的 OAuth、GitHub 连接或其他正常授权，将该文件移动至 `.github/workflows/ci.yml` 并提交后，才会触发 CI。是否通过以该提交的 Actions 实际结果为准。
