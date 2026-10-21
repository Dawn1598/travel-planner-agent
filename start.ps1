$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw '创建 Python 环境失败' }
}
& ./.venv/Scripts/python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw '安装依赖失败' }
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
    throw '请先填写 .env 中的密钥，再启动。'
}
& ./.venv/Scripts/python.exe -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw '安装采集浏览器失败' }
& ./.venv/Scripts/python.exe manage.py migrate
if ($LASTEXITCODE -ne 0) { throw '数据库初始化失败' }
& ./.venv/Scripts/python.exe manage.py runserver 127.0.0.1:8017 --noreload
