# 服务器部署

使用独立的 Nginx、Gunicorn 和 Supervisor 运行 Django。默认 Nginx 监听 8017，Gunicorn 仅监听 `127.0.0.1:18017`。下面是部署模板，请使用自己的域名、服务器和配置。

## 目录

```text
/opt/travel-planner/
  app/                 代码和静态文件
  venv/                Python 环境
  shared/.env          后端密钥与部署配置
  shared/db.sqlite3    数据库
  shared/runtime/     浏览器登录状态与向量模型缓存
  run/                 PID、Unix socket
  logs/                服务日志
```

## 首次安装

服务器需具备兼容的 Python 3.10+（建议 3.12）、venv、Nginx、Supervisor、curl，以及安装浏览器系统依赖的权限。提供的浏览器安装脚本面向 Ubuntu/Debian x86_64，采用独立解包的 Chrome；其他平台需相应调整。

1. 在本地完成 `.env` 配置和数据库初始化。需要小红书采集时，先在本机完成正常登录。
2. 打包代码与私有运行数据：

   ```powershell
   .\.venv\Scripts\python.exe scripts/package_deploy.py --hosts travel.example.com
   ```

3. 将 `.runtime/deployment/code.tar.gz` 和 `private.tar.gz` 通过安全传输放入服务器 `/opt/travel-planner/incoming/`，限制私有包的读取权限，再解出安装脚本并执行首次安装：

   ```sh
   cd /opt/travel-planner/incoming
   chmod 600 private.tar.gz
   tar -xzf code.tar.gz deploy/install.sh
   # 以有权限安装系统依赖及写入 /opt 的用户执行
   PYTHON_BIN=python3.12 sh deploy/install.sh
   travelctl start
   ```

4. 按自己的服务器配置安全组、防火墙、域名与 HTTPS。检查 `shared/.env` 中的 `DJANGO_ALLOWED_HOSTS`；生产环境使用 `DJANGO_DEBUG=0`。HTTPS 部署还应设置 Django 的安全 Cookie 和可信代理配置。

`code.tar.gz` 是代码包；`private.tar.gz` 含密钥、数据库、用户会话与小红书登录状态，不能上传 GitHub、Release 或静态文件目录。打包脚本不会自动上传或发布它们。

安装脚本使用 `PYTHON_BIN` 指定的解释器，默认 `python3`，不依赖其他项目的 Python 环境。已有安装会被脚本拒绝，避免误覆盖数据。

## 日常维护

```sh
travelctl status
travelctl restart
travelctl stop
travelctl start
tail -n 100 /opt/travel-planner/logs/web.log
```

更新时先备份服务器 `.env`、数据库及运行目录，再更新代码、安装依赖、执行 `migrate` 和 `collectstatic`，最后重启。不要用本地数据快照覆盖线上数据库。操作系统开机自启需结合所在主机的服务管理方式自行配置。

Gunicorn 使用单 worker、多线程与进程内任务池。启动脚本会将中断任务标记为可重试，不会自动重放模型请求。进程重启会中断正在规划的任务，已保存的会话与时间表仍保留。

## 小红书与浏览器

服务器默认关闭交互式登录窗口（`XHS_LOGIN_ENABLED=0`），使用私有同步的登录状态。状态过期或平台要求验证时，应在本机完成登录后安全同步 `.runtime/xhs-state.json`。不可将 Cookie 写入 README、代码或公开日志。

`XHS_BROWSER_EXECUTABLE` 可指定 Chrome 可执行文件。安装脚本中的 Ubuntu 20.04 兼容方式将 Chrome 解包至 `shared/browser/`，不替换系统已有浏览器。实际是否可采集仍取决于登录状态与页面访问条件。

## 验证

```sh
/opt/travel-planner/venv/bin/python /opt/travel-planner/app/manage.py check
/opt/travel-planner/venv/bin/python /opt/travel-planner/app/manage.py test planner
curl -I http://127.0.0.1:8017/
```

本机 HTTP 可访问不等于公网已放行，需要从外部另行验证。真实服务连通检查可运行 `manage.py check_services`，该命令会调用配置的外部服务。
