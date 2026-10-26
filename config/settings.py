import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', '')
if not SECRET_KEY:
    raise RuntimeError('请在 .env 中设置 DJANGO_SECRET_KEY')
DEBUG = os.getenv('DJANGO_DEBUG', '0') == '1'
ALLOWED_HOSTS = os.getenv('DJANGO_ALLOWED_HOSTS', '127.0.0.1,localhost').split(',')
INSTALLED_APPS = ['django.contrib.contenttypes', 'django.contrib.sessions', 'django.contrib.staticfiles', 'planner']
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]
ROOT_URLCONF = 'config.urls'
TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [BASE_DIR / 'templates'], 'APP_DIRS': True,
    'OPTIONS': {'context_processors': ['django.template.context_processors.request']},
}]
WSGI_APPLICATION = 'config.wsgi.application'
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': BASE_DIR / 'db.sqlite3', 'OPTIONS': {'timeout': 30}}}
LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_TZ = True
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_NAME = 'travel_sessionid'
CSRF_COOKIE_NAME = 'travel_csrftoken'
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
SECURE_CONTENT_TYPE_NOSNIFF = True
DATA_UPLOAD_MAX_MEMORY_SIZE = 64 * 1024
RUNTIME_DIR = BASE_DIR / '.runtime'
RUNTIME_DIR.mkdir(exist_ok=True)

DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', '')
DEEPSEEK_MODEL = os.getenv('DEEPSEEK_MODEL', 'deepseek-flash')
DEEPSEEK_BASE_URL = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')
AMAP_API_KEY = os.getenv('AMAP_API_KEY', '')
EMBEDDING_MODEL = os.getenv('EMBEDDING_MODEL', 'BAAI/bge-small-zh-v1.5')
MAX_AGENT_STEPS = int(os.getenv('MAX_AGENT_STEPS', '20'))
MAX_REVISIONS = int(os.getenv('MAX_REVISIONS', '2'))
XHS_CANDIDATE_LIMIT = int(os.getenv('XHS_CANDIDATE_LIMIT', '200'))
XHS_TARGET_NEW = int(os.getenv('XHS_TARGET_NEW', '30'))
XHS_MAX_VISITS = int(os.getenv('XHS_MAX_VISITS', '240'))
XHS_TIME_BUDGET_SECONDS = float(os.getenv('XHS_TIME_BUDGET_SECONDS', '600'))
XHS_RETRIES = int(os.getenv('XHS_RETRIES', '1'))
XHS_STAGNANT_ROUNDS = int(os.getenv('XHS_STAGNANT_ROUNDS', '3'))
XHS_REFRESH_HOURS = int(os.getenv('XHS_REFRESH_HOURS', '24'))
XHS_LOGIN_ENABLED = os.getenv('XHS_LOGIN_ENABLED', '1') == '1'
XHS_BROWSER_EXECUTABLE = os.getenv('XHS_BROWSER_EXECUTABLE', '')

# Never allow HTTP SDK debug logs to print authenticated MCP URLs.
LOGGING = {'version': 1, 'disable_existing_loggers': False, 'loggers': {
    name: {'level': 'CRITICAL'} for name in ['httpx', 'httpcore', 'mcp', 'openai']
}}
