#!/usr/bin/env python3
"""Profile sync for github.com/aditya30-2005-eng.

Run by .github/workflows/profile-sync.yml. It scans every public repo you own,
works out the stack each one uses (from requirements.txt, package.json, imports,
Dockerfiles, CI files, topics and GitHub's language stats), then:

  * regenerates stack.svg      - your curated stack + anything new it finds
  * regenerates latest-repos.svg - your most recently updated repos as cards
  * rewrites the block between the PROFILE-SYNC markers in README.md
  * bumps the ?v= cache keys so GitHub shows the new images straight away

Edit profile.config.json to hide a technology, hide a repo, or rename one.
"""
import argparse, base64, bisect, hashlib, io, itertools, json, math, os, re, sys, tempfile, time
import urllib.error, urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

from fontTools import subset as ftsubset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

HERE = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------------ palette
NAVY, PANEL, PANEL2, LINE = '#070b16', '#0c1428', '#101a31', '#1c2742'
BLUE, RED, WHITE, MUTED, SOFT = '#247bff', '#ff354f', '#f3f5fa', '#8a95ad', '#b4bdd0'
GREEN = '#5fe39a'

# ------------------------------------------------------------------ pinned, licensed assets
FONT_BASE = 'https://raw.githubusercontent.com/google/fonts/6157dfaa640ca5738b4d1a7070fe389c3036d7fc/ofl/'
FONT_FILES = {  # key: (path under ofl/, css family, weight)  - all SIL Open Font License 1.1
    'd8': ('barlowcondensed/BarlowCondensed-ExtraBold.ttf', 'AKSD8', 800),
    'd6': ('barlowcondensed/BarlowCondensed-SemiBold.ttf', 'AKSD6', 600),
    'b4': ('barlow/Barlow-Regular.ttf', 'AKSB4', 400),
    'm5': ('jetbrainsmono/JetBrainsMono[wght].ttf', 'AKSM5', 500),
}
FALLBACK = {'d': "'Arial Narrow',sans-serif", 'b': 'Arial,sans-serif', 'm': "'DejaVu Sans Mono',monospace"}
ICON_BASE = 'https://raw.githubusercontent.com/simple-icons/simple-icons/16.36.0/icons/'  # CC0 1.0
ICON_HEX = {'python': '3776AB', 'django': '092E20', 'postgresql': '4169E1', 'supabase': '3FCF8E', 'react': '61DAFB',
            'nodedotjs': '5FA04E', 'javascript': 'F7DF1E', 'redis': 'FF4438', 'celery': '37814A', 'mongodb': '47A248',
            'docker': '2496ED', 'githubactions': '2088FF', 'git': 'F03C2E', 'linux': 'FCC624', 'googlecloud': '4285F4',
            'apachespark': 'E25A1C', 'databricks': 'FF3621', 'scikitlearn': 'F7931E', 'flask': '3BABC3', 'fastapi': '009688',
            'nextdotjs': '000000', 'vuedotjs': '4FC08D', 'tailwindcss': '06B6D4', 'vite': '9135FF', 'firebase': 'DD2C00',
            'mysql': '4479A1', 'sqlite': '003B57', 'tensorflow': 'FF6F00', 'pytorch': 'EE4C2C', 'pandas': '150458',
            'numpy': '013243', 'streamlit': 'FF4B4B', 'jupyter': 'F37626', 'opencv': '5C3EE8', 'apachekafka': '231F20',
            'apacheairflow': '017CEE', 'kubernetes': '326CE5', 'terraform': '844FBA', 'render': '000000', 'netlify': '00C7B7',
            'vercel': '000000', 'springboot': '6DB33F', 'go': '00ADD8', 'kotlin': '7F52FF', 'rust': '000000', 'php': '777BB4',
            'dart': '0175C2', 'gnubash': '4EAA25', 'graphql': 'E10098', 'prisma': '2D3748', 'socketdotio': '010101',
            'redux': '764ABC', 'pytest': '0A9EDC', 'jest': 'C21325', 'selenium': '43B02A', 'postman': 'FF6C37',
            'razorpay': '0C2451', 'stripe': '635BFF', 'huggingface': 'FFD21E', 'langchain': '7FC8FF', 'openjdk': '000000',
            'express': '0A0A0A'}
ICON_LABEL = {'nodedotjs': 'Node.js', 'githubactions': 'GitHub Actions', 'googlecloud': 'Google Cloud', 'apachespark': 'Apache Spark',
              'scikitlearn': 'scikit-learn', 'postgresql': 'PostgreSQL', 'mongodb': 'MongoDB', 'javascript': 'JavaScript'}

# GitHub's own language colours, for the dot on each repo card
LANG_COLOR = {'Python': '#3572A5', 'JavaScript': '#f1e05a', 'Java': '#b07219', 'HTML': '#e34c26', 'CSS': '#563d7c',
              'Jupyter Notebook': '#DA5B0B', 'TypeScript': '#3178c6', 'Shell': '#89e051', 'C': '#555555', 'C++': '#f34b7d',
              'Go': '#00ADD8', 'Kotlin': '#A97BFF', 'Dart': '#00B4AB', 'PHP': '#4F5D95', 'Rust': '#dea584',
              'Dockerfile': '#384d54', 'C#': '#178600', 'Apex': '#1797c0', 'SCSS': '#c6538c'}

# ------------------------------------------------------------------ technology catalogue
# label -> (group, also-counts-as labels). Groups: backend data frontend db devops quality lang
T = {
    'Django': ('backend', []), 'Django REST Framework': ('backend', ['DRF']), 'Simple JWT': ('backend', []),
    'django-filter': ('backend', []), 'Flask': ('backend', []), 'FastAPI': ('backend', []), 'Node.js': ('backend', []),
    'Express.js': ('backend', ['Express']), 'Spring Boot': ('backend', []), 'GraphQL': ('backend', []), 'Socket.IO': ('backend', []),
    'SQLAlchemy': ('backend', []), 'Prisma': ('backend', []), 'REST APIs': ('backend', []), 'JWT': ('backend', ['Simple JWT', 'JWT & RBAC']),
    'Swagger': ('quality', []),
    'React.js': ('frontend', ['React']), 'Next.js': ('frontend', []), 'Vue.js': ('frontend', []), 'Angular': ('frontend', []),
    'Vite': ('frontend', []), 'Tailwind CSS': ('frontend', []), 'Axios': ('frontend', []), 'React Router': ('frontend', []),
    'Redux': ('frontend', []), 'HTML5': ('frontend', ['HTML']), 'CSS3': ('frontend', ['CSS']), 'Bootstrap': ('frontend', []),
    'PostgreSQL': ('db', []), 'Supabase': ('db', []), 'MySQL': ('db', []), 'SQL Server': ('db', []), 'MongoDB': ('db', []),
    'Redis': ('db', []), 'SQLite': ('db', []), 'Firebase': ('db', []), 'Firestore': ('db', []),
    'Pandas': ('data', []), 'NumPy': ('data', []), 'scikit-learn': ('data', []), 'PySpark': ('data', ['Apache Spark']),
    'Databricks': ('data', []), 'Delta Lake': ('data', []), 'Streamlit': ('data', []), 'TensorFlow': ('data', []),
    'PyTorch': ('data', []), 'Keras': ('data', []), 'OpenCV': ('data', []), 'Matplotlib': ('data', []), 'Seaborn': ('data', []),
    'Plotly': ('data', []), 'Jupyter': ('data', []), 'Apache Kafka': ('data', []), 'Apache Airflow': ('data', []),
    'LangChain': ('data', []), 'Hugging Face': ('data', []), 'OpenAI API': ('data', []), 'ETL': ('data', []),
    'Celery': ('devops', []), 'Docker': ('devops', ['Docker Compose']), 'GitHub Actions CI': ('devops', ['GitHub Actions']),
    'Render': ('devops', []), 'Netlify': ('devops', []), 'Vercel': ('devops', []), 'Kubernetes': ('devops', []),
    'Terraform': ('devops', []), 'Nginx': ('devops', []), 'Gunicorn': ('devops', []), 'AWS': ('devops', []), 'GCP': ('devops', []),
    'pytest': ('quality', []), 'Jest': ('quality', []), 'Selenium': ('quality', []), 'Razorpay': ('quality', []),
    'Stripe': ('quality', []), 'Postman': ('quality', []),
    'Python': ('lang', []), 'Java': ('lang', []), 'JavaScript': ('lang', []), 'SQL': ('lang', []), 'TypeScript': ('lang', []),
    'C#': ('lang', []), 'Go': ('lang', []), 'Kotlin': ('lang', []), 'C++': ('lang', []), 'C': ('lang', []), 'Rust': ('lang', []),
    'PHP': ('lang', []), 'Dart': ('lang', []), 'Bash': ('lang', []), 'Apex': ('lang', []),
}
GROUP_ORDER = ['backend', 'data', 'frontend', 'db', 'devops', 'quality', 'lang']

PY_PKG = {'django': 'Django', 'djangorestframework': 'Django REST Framework', 'djangorestframework-simplejwt': 'Simple JWT',
          'django-filter': 'django-filter', 'drf-spectacular': 'Swagger', 'drf-yasg': 'Swagger', 'flask': 'Flask',
          'fastapi': 'FastAPI', 'celery': 'Celery', 'redis': 'Redis', 'psycopg': 'PostgreSQL', 'psycopg2': 'PostgreSQL',
          'psycopg2-binary': 'PostgreSQL', 'asyncpg': 'PostgreSQL', 'pymongo': 'MongoDB', 'mongoengine': 'MongoDB',
          'mysqlclient': 'MySQL', 'pymysql': 'MySQL', 'mysql-connector-python': 'MySQL', 'pyodbc': 'SQL Server',
          'supabase': 'Supabase', 'sqlalchemy': 'SQLAlchemy', 'pandas': 'Pandas', 'numpy': 'NumPy', 'scikit-learn': 'scikit-learn',
          'sklearn': 'scikit-learn', 'pyspark': 'PySpark', 'delta-spark': 'Delta Lake', 'databricks-connect': 'Databricks',
          'databricks-sdk': 'Databricks', 'streamlit': 'Streamlit', 'tensorflow': 'TensorFlow', 'torch': 'PyTorch',
          'keras': 'Keras', 'opencv-python': 'OpenCV', 'opencv-python-headless': 'OpenCV', 'matplotlib': 'Matplotlib',
          'seaborn': 'Seaborn', 'plotly': 'Plotly', 'jupyter': 'Jupyter', 'notebook': 'Jupyter', 'kafka-python': 'Apache Kafka',
          'confluent-kafka': 'Apache Kafka', 'apache-airflow': 'Apache Airflow', 'langchain': 'LangChain',
          'transformers': 'Hugging Face', 'openai': 'OpenAI API', 'pytest': 'pytest', 'selenium': 'Selenium',
          'razorpay': 'Razorpay', 'stripe': 'Stripe', 'gunicorn': 'Gunicorn', 'boto3': 'AWS', 'google-cloud-storage': 'GCP'}
JS_PKG = {'react': 'React.js', 'next': 'Next.js', 'vue': 'Vue.js', '@angular/core': 'Angular', 'vite': 'Vite',
          'tailwindcss': 'Tailwind CSS', 'axios': 'Axios', 'react-router-dom': 'React Router', 'react-router': 'React Router',
          'redux': 'Redux', '@reduxjs/toolkit': 'Redux', 'bootstrap': 'Bootstrap', 'express': 'Express.js',
          'mongoose': 'MongoDB', 'mongodb': 'MongoDB', 'pg': 'PostgreSQL', 'mysql2': 'MySQL', 'mysql': 'MySQL',
          'redis': 'Redis', 'ioredis': 'Redis', '@supabase/supabase-js': 'Supabase', 'firebase': 'Firebase',
          'razorpay': 'Razorpay', 'stripe': 'Stripe', 'jsonwebtoken': 'JWT', 'socket.io': 'Socket.IO', 'graphql': 'GraphQL',
          'prisma': 'Prisma', '@prisma/client': 'Prisma', 'jest': 'Jest', 'typescript': 'TypeScript', 'sqlite3': 'SQLite'}
PY_IMPORT = {'django': 'Django', 'rest_framework': 'Django REST Framework', 'flask': 'Flask', 'fastapi': 'FastAPI',
             'pandas': 'Pandas', 'numpy': 'NumPy', 'sklearn': 'scikit-learn', 'pyspark': 'PySpark', 'delta': 'Delta Lake',
             'streamlit': 'Streamlit', 'tensorflow': 'TensorFlow', 'torch': 'PyTorch', 'keras': 'Keras', 'cv2': 'OpenCV',
             'matplotlib': 'Matplotlib', 'seaborn': 'Seaborn', 'plotly': 'Plotly', 'kafka': 'Apache Kafka',
             'airflow': 'Apache Airflow', 'langchain': 'LangChain', 'transformers': 'Hugging Face', 'openai': 'OpenAI API',
             'celery': 'Celery', 'pymongo': 'MongoDB', 'psycopg2': 'PostgreSQL', 'psycopg': 'PostgreSQL', 'sqlite3': 'SQLite',
             'supabase': 'Supabase', 'selenium': 'Selenium', 'razorpay': 'Razorpay'}
LANG_TECH = {'Python': 'Python', 'JavaScript': 'JavaScript', 'Java': 'Java', 'HTML': 'HTML5', 'CSS': 'CSS3', 'SCSS': 'CSS3',
             'Jupyter Notebook': 'Jupyter', 'TypeScript': 'TypeScript', 'C#': 'C#', 'Go': 'Go', 'Kotlin': 'Kotlin',
             'C++': 'C++', 'C': 'C', 'Rust': 'Rust', 'PHP': 'PHP', 'Dart': 'Dart', 'Shell': 'Bash', 'Dockerfile': 'Docker',
             'PLpgSQL': 'SQL', 'TSQL': 'SQL', 'Apex': 'Apex'}
TOPIC_TECH = {re.sub(r'[^a-z0-9]', '', k.lower()): k for k in T}
TOPIC_TECH.update({'react': 'React.js', 'reactjs': 'React.js', 'nodejs': 'Node.js', 'express': 'Express.js', 'expressjs': 'Express.js',
                   'drf': 'Django REST Framework', 'spark': 'PySpark', 'deltalake': 'Delta Lake', 'sklearn': 'scikit-learn',
                   'machinelearning': 'scikit-learn', 'githubactions': 'GitHub Actions CI', 'postgres': 'PostgreSQL',
                   'tailwind': 'Tailwind CSS', 'nextjs': 'Next.js', 'vuejs': 'Vue.js', 'etl': 'ETL'})
SKIP_DIRS = {'node_modules', 'venv', '.venv', 'env', 'dist', 'build', 'site-packages', '__pycache__', '.next', 'vendor', '.git'}

# ------------------------------------------------------------------ fetching
def http_get_full(url, token=None, accept=None, tries=3):
    """(body, headers); body is None for 404/409 (missing or empty repo)."""
    headers = {'User-Agent': 'profile-sync'}
    if token: headers['Authorization'] = f'Bearer {token}'
    if accept: headers['Accept'] = accept
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                return r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            if e.code in (404, 409, 451): return None, {}
            if k == tries - 1: raise
        except urllib.error.URLError:
            if k == tries - 1: raise
        time.sleep(2 * (k + 1))

def http_get(url, token=None, accept=None, tries=3):
    return http_get_full(url, token, accept, tries)[0]

WEEKS = 12

def week_start(dt):
    return (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)

def bucket_weeks(dates, now):
    """Commit datetimes -> counts for the last WEEKS calendar weeks (Mon-Sun, UTC), oldest first."""
    w0, out = week_start(now), [0] * WEEKS
    for dt in dates:
        ago = (w0 - week_start(dt)).days // 7
        if 0 <= ago < WEEKS: out[WEEKS - 1 - ago] += 1
    return out

def parse_dt(s):
    return datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone(timezone.utc)

class GitHubSource:
    def __init__(self, user, token):
        self.user, self.token = user, token
    def api(self, path):
        raw = http_get('https://api.github.com' + path, self.token, 'application/vnd.github+json')
        return json.loads(raw) if raw else None
    def repos(self):
        out, page = [], 1
        while True:
            batch = self.api(f'/users/{self.user}/repos?type=owner&sort=pushed&per_page=100&page={page}') or []
            out += batch
            if len(batch) < 100: return out
            page += 1
    def languages(self, repo):
        return self.api(f'/repos/{self.user}/{repo["name"]}/languages') or {}
    def tree(self, repo):
        t = self.api(f'/repos/{self.user}/{repo["name"]}/git/trees/{repo["default_branch"]}?recursive=1')
        return [(e['path'], e.get('size', 0)) for e in (t or {}).get('tree', []) if e.get('type') == 'blob']
    def read(self, repo, path):
        from urllib.parse import quote
        raw = http_get(f'https://raw.githubusercontent.com/{self.user}/{repo["name"]}/{repo["default_branch"]}/{quote(path)}')
        return raw.decode('utf-8', 'replace') if raw else ''
    def commit_stats(self, repo, now):
        base = f'https://api.github.com/repos/{self.user}/{repo["name"]}/commits?sha={repo["default_branch"]}'
        body, hdr = http_get_full(base + '&per_page=1', self.token, 'application/vnd.github+json')
        if not body: return dict(total=0, weekly=[0] * WEEKS, last=None)
        first = json.loads(body)
        m = re.search(r'[?&]page=(\d+)>; rel="last"', hdr.get('Link', '') or hdr.get('link', ''))
        total = int(m.group(1)) if m else len(first)
        last = parse_dt(first[0]['commit']['author']['date']) if first else None
        since = (week_start(now) - timedelta(weeks=WEEKS - 1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        dates, page = [], 1
        while page <= 10:
            batch = json.loads(http_get(f'{base}&since={since}&per_page=100&page={page}', self.token,
                                        'application/vnd.github+json') or b'[]')
            dates += [parse_dt(c['commit']['author']['date']) for c in batch]
            if len(batch) < 100: break
            page += 1
        return dict(total=total, weekly=bucket_weeks(dates, now), last=last)

class LocalSource:
    """Offline test double: folders of cloned repos + a meta.json with API-style fields."""
    EXT = {'.py': 'Python', '.js': 'JavaScript', '.jsx': 'JavaScript', '.html': 'HTML', '.css': 'CSS', '.ipynb': 'Jupyter Notebook',
           '.ts': 'TypeScript', '.tsx': 'TypeScript', '.java': 'Java', '.sh': 'Shell'}
    def __init__(self, root):
        self.root = root; self.meta = json.load(open(os.path.join(root, 'meta.json')))
    def repos(self): return self.meta
    def _files(self, repo):
        base = os.path.join(self.root, repo['name'])
        for dp, dns, fns in os.walk(base):
            dns[:] = [x for x in dns if x not in SKIP_DIRS]
            for f in fns:
                full = os.path.join(dp, f); yield os.path.relpath(full, base), os.path.getsize(full)
    def languages(self, repo):
        out = defaultdict(int)
        for p, s in self._files(repo):
            lang = self.EXT.get(os.path.splitext(p)[1].lower())
            if lang: out[lang] += s
        return dict(out)
    def tree(self, repo): return list(self._files(repo))
    def read(self, repo, path):
        return open(os.path.join(self.root, repo['name'], path), encoding='utf-8', errors='replace').read()
    def commit_stats(self, repo, now):
        import subprocess
        git = lambda *a: subprocess.run(['git', '-C', os.path.join(self.root, repo['name']), *a],
                                        capture_output=True, text=True).stdout.strip()
        dates = [parse_dt(x) for x in git('log', '--format=%aI').splitlines() if x]
        return dict(total=len(dates), weekly=bucket_weeks(dates, now), last=max(dates) if dates else None)

# ------------------------------------------------------------------ stack detection
def py_names(text):
    for line in text.splitlines():
        line = line.split('#')[0].strip()
        if not line or line.startswith(('-', '[')): continue
        m = re.match(r'([A-Za-z0-9_.\-]+)', line)
        if m: yield m.group(1).lower().replace('_', '-')

def pyproject_names(text):
    for m in re.finditer(r'["\']([A-Za-z0-9_.\-]+)\s*(?:\[[^\]]*\])?\s*(?:[<>=!~;].*?)?["\']', text):
        yield m.group(1).lower().replace('_', '-')
    for m in re.finditer(r'^\s*([A-Za-z0-9_.\-]+)\s*=', text, re.M):
        yield m.group(1).lower().replace('_', '-')

def detect(src, repo):
    found = set()
    langs = src.languages(repo)
    total = sum(langs.values()) or 1
    for lang, n in langs.items():
        if n / total >= 0.03 and lang in LANG_TECH: found.add(LANG_TECH[lang])
    for t in repo.get('topics') or []:
        k = re.sub(r'[^a-z0-9]', '', t.lower())
        if k in TOPIC_TECH: found.add(TOPIC_TECH[k])
    files = src.tree(repo)
    def ok(p):
        parts = p.split('/')
        return len(parts) <= 3 and not (set(parts[:-1]) & SKIP_DIRS)
    paths = [(p, s) for p, s in files if ok(p)]
    names = {p.split('/')[-1].lower() for p, _ in paths}
    lower = [p.lower() for p, _ in paths]
    if 'manage.py' in names: found.add('Django')
    if 'dockerfile' in names or any(n.startswith('docker-compose') or n.startswith('compose.') for n in names): found.add('Docker')
    if any(p.startswith('.github/workflows/') for p in lower): found.add('GitHub Actions CI')
    if 'render.yaml' in names: found.add('Render')
    if 'netlify.toml' in names: found.add('Netlify')
    if 'vercel.json' in names: found.add('Vercel')
    if 'pom.xml' in names or 'build.gradle' in names: found.add('Java')
    reads = 0
    for p, s in paths:
        n = p.split('/')[-1].lower()
        if s > 400_000 or reads > 24: continue
        if n.startswith('requirements') and n.endswith('.txt') or n == 'pipfile':
            reads += 1; found |= {PY_PKG[x] for x in py_names(src.read(repo, p)) if x in PY_PKG}
        elif n == 'pyproject.toml':
            reads += 1; found |= {PY_PKG[x] for x in pyproject_names(src.read(repo, p)) if x in PY_PKG}
        elif n == 'package.json':
            reads += 1
            try:
                pj = json.loads(src.read(repo, p))
                for k in list((pj.get('dependencies') or {})) + list((pj.get('devDependencies') or {})):
                    if k in JS_PKG: found.add(JS_PKG[k])
                if 'express' in (pj.get('dependencies') or {}) or 'Express.js' in found: found.add('Node.js')
            except ValueError:
                pass
    # look inside a few code files for imports (catches notebooks and plain-JS projects)
    code = [(p, s) for p, s in files if p.endswith(('.py', '.ipynb', '.js', '.jsx', '.html')) and s < 300_000
            and not (set(p.split('/')[:-1]) & SKIP_DIRS)]
    code.sort(key=lambda x: (x[0].endswith('.html'), x[0].count('/'), x[1]))
    for p, _ in code[:14]:
        text = src.read(repo, p)
        for m in re.finditer(r'(?:^|\\n|["\s])(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)', text):
            if m.group(1) in PY_IMPORT: found.add(PY_IMPORT[m.group(1)])
        if re.search(r'firebasejs/|from\s+["\']firebase', text): found.add('Firebase')
        if re.search(r'firestore', text, re.I): found.add('Firestore')
        if re.search(r'require\(["\']express["\']\)|from\s+["\']express["\']', text): found |= {'Express.js', 'Node.js'}
        if re.search(r'require\(["\']mongoose["\']\)|from\s+["\']mongoose["\']', text): found.add('MongoDB')
    if found & {'Django REST Framework', 'FastAPI', 'Express.js', 'Flask'}: found.add('REST APIs')
    readme = next((p for p, _ in files if p.lower() in ('readme.md', 'readme.rst', 'readme.txt')), None)
    repo['_readme_blurb'] = readme_blurb(src.read(repo, readme)) if readme else ''
    return found

def readme_blurb(text):
    """First real prose paragraph of a README, stripped of markdown."""
    for para in re.split(r'\n\s*\n', text):
        para = para.strip()
        if not para or para.startswith(('#', '!', '<', '|', '```', '---', '-', '*', '>')) or para.count('[![') : continue
        para = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', para)
        para = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', para)
        para = re.sub(r'[*_`]', '', ' '.join(para.split()))
        if len(para) >= 40: return para
    return ''

# headline technologies first on repo cards; supporting libraries after them
KEY_TECH = ['Django', 'FastAPI', 'Flask', 'Spring Boot', 'Express.js', 'Node.js', 'Next.js', 'React.js', 'Vue.js', 'Angular',
            'PySpark', 'Databricks', 'Delta Lake', 'TensorFlow', 'PyTorch', 'scikit-learn', 'LangChain', 'Hugging Face',
            'Streamlit', 'Apache Kafka', 'Apache Airflow', 'PostgreSQL', 'Supabase', 'MongoDB', 'MySQL', 'Firebase', 'Redis',
            'Celery', 'Docker', 'Django REST Framework', 'Pandas', 'NumPy', 'OpenCV', 'Kubernetes', 'Terraform', 'GraphQL',
            'Razorpay', 'Stripe', 'Tailwind CSS', 'GitHub Actions CI', 'SQLite', 'Java', 'Go', 'Kotlin']
MINOR = {'REST APIs', 'Python', 'JavaScript', 'HTML5', 'CSS3', 'Jupyter', 'Gunicorn', 'Bash', 'SQL'}

def ordered(techs):
    def key(t):
        if t in KEY_TECH: return (0, KEY_TECH.index(t), '')
        return (2 if t in MINOR else 1, GROUP_ORDER.index(T.get(t, ('lang',))[0]), t.lower())
    return sorted(techs, key=key)

# ------------------------------------------------------------------ fonts, icons, svg
class Assets:
    def __init__(self, local=None, cache=None):
        self.local = local
        self.cache = cache or os.path.join(tempfile.gettempdir(), 'profile-sync-cache')
        os.makedirs(self.cache, exist_ok=True)
        self._fonts, self._icons = {}, {}
    def _get(self, url, local_path, name):
        if self.local:
            return open(local_path, 'rb').read()
        p = os.path.join(self.cache, name)
        if not os.path.exists(p):
            data = http_get(url)
            if not data: raise RuntimeError('could not download ' + url)
            open(p, 'wb').write(data)
        return open(p, 'rb').read()
    def font(self, key):
        if key not in self._fonts:
            rel = FONT_FILES[key][0]
            data = self._get(FONT_BASE + rel.replace('[', '%5B').replace(']', '%5D'),
                             os.path.join(self.local or '', 'gfonts', 'ofl', rel), rel.replace('/', '_'))
            f = TTFont(io.BytesIO(data), recalcTimestamp=False)  # fixed timestamps = identical output each run
            if key == 'm5':
                f = instancer.instantiateVariableFont(f, {'wght': 500})
                f.recalcTimestamp = False
                b = io.BytesIO(); f.save(b); b.seek(0); f = TTFont(b, recalcTimestamp=False)
            self._fonts[key] = f
        return self._fonts[key]
    def icon(self, slug):
        if slug not in self._icons:
            svg = self._get(ICON_BASE + slug + '.svg', os.path.join(self.local or '', 'si', 'icons', slug + '.svg'),
                            'icon_' + slug + '.svg').decode()
            m = re.search(r'<path d="([^"]+)"', svg)
            self._icons[slug] = m.group(1) if m else ''
        return self._icons[slug]

A = None  # set in main()

def tw(key, text, size, ls=0.0):
    f = A.font(key); cmap = f.getBestCmap(); hm = f['hmtx']; upm = f['head'].unitsPerEm
    return sum(hm[cmap.get(ord(c), '.notdef')][0] for c in text) * size / upm + ls * size * len(text)

def fit(key, text, size, maxw, ls=0.0):
    while size > 6 and tw(key, text, size, ls) > maxw: size -= .5
    return size

def wrap(key, text, size, maxw, max_lines=99):
    words, lines, cur = text.split(), [], ''
    for w in words:
        trial = (cur + ' ' + w).strip()
        if tw(key, trial, size) <= maxw: cur = trial
        else: lines.append(cur); cur = w
    if cur: lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and tw(key, lines[-1] + '…', size) > maxw: lines[-1] = lines[-1][:-1].rstrip()
        lines[-1] += '…'
    return lines

def woff2_b64(key, chars):
    b = io.BytesIO(); A.font(key).save(b); b.seek(0); font = TTFont(b, recalcTimestamp=False)
    o = ftsubset.Options(); o.flavor = 'woff2'; o.layout_features = ['kern', 'liga', 'calt']; o.name_IDs = ['*']
    o.notdef_outline = True; o.hinting = False
    s = ftsubset.Subsetter(o); s.populate(text=''.join(sorted(chars)) + ' '); s.subset(font)
    out = io.BytesIO(); font.flavor = 'woff2'; font.save(out)
    return base64.b64encode(out.getvalue()).decode()

def lum(h):
    r, g, b = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    f = lambda c: c / 12.92 if c <= .03928 else ((c + .055) / 1.055) ** 2.4
    return .2126 * f(r) + .7152 * f(g) + .0722 * f(b)

def icon_color(slug):
    h = ICON_HEX.get(slug, 'f3f5fa')
    return '#' + h if lum(h) > 0.06 else WHITE

def d(s): return f'style="animation-delay:{s:.2f}s"'

def rise_css(p):
    return (f'@keyframes {p}-rise{{from{{opacity:0;transform:translateY(18px)}}to{{opacity:1;transform:none}}}}\n'
            f'@keyframes {p}-fade{{from{{opacity:0}}to{{opacity:1}}}}\n'
            f'.{p}-rise{{animation:{p}-rise .8s cubic-bezier(.2,.8,.2,1) both}}\n.{p}-fade{{animation:{p}-fade .9s ease both}}')

class SVG:
    def __init__(self, p, w, h, title, desc):
        self.p, self.w, self.h, self.title, self.desc = p, w, h, title, desc
        self.defs, self.body, self.css, self.used = [], [], [], defaultdict(set)
    def t(self, x, y, text, font='b4', size=16, fill=WHITE, anchor='start', ls=0.0):
        self.used[font].update(text)
        st = f"font-family:{FONT_FILES[font][1]},{FALLBACK[font[0]]};font-size:{size}px;font-weight:{FONT_FILES[font][2]}"
        if ls: st += f';letter-spacing:{ls}em'
        a = '' if anchor == 'start' else f' text-anchor="{anchor}"'
        return f'<text x="{x:.1f}" y="{y:.1f}"{a} fill="{fill}" style="{st}">{escape(text)}</text>'
    def background(self, glows=(), rx=28):
        p = self.p
        self.defs.append(f'''<linearGradient id="{p}-edge" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{BLUE}" stop-opacity=".85"/><stop offset=".5" stop-color="#26355a" stop-opacity=".55"/><stop offset="1" stop-color="{RED}" stop-opacity=".85"/></linearGradient>
<pattern id="{p}-dots" width="22" height="22" patternUnits="userSpaceOnUse"><circle cx="3" cy="3" r="1.1" fill="#c8d4ff" fill-opacity=".085"/></pattern>
<clipPath id="{p}-frame"><rect x="1" y="1" width="{self.w - 2}" height="{self.h - 2}" rx="{rx}"/></clipPath>
<radialGradient id="{p}-gb" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="{BLUE}" stop-opacity=".30"/><stop offset="1" stop-color="{BLUE}" stop-opacity="0"/></radialGradient>
<radialGradient id="{p}-gr" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="{RED}" stop-opacity=".24"/><stop offset="1" stop-color="{RED}" stop-opacity="0"/></radialGradient>''')
        g = ''.join(f'<ellipse cx="{x}" cy="{y}" rx="{r}" ry="{r * .8:.0f}" fill="url(#{p}-g{c})"/>' for x, y, r, c in glows)
        sp = round(self.w * .66)
        self.body.append(f'<g clip-path="url(#{p}-frame)"><rect width="{self.w}" height="{self.h}" fill="{NAVY}"/>'
                         f'<rect width="{self.w}" height="{self.h}" fill="url(#{p}-dots)"/>{g}'
                         f'<rect width="{sp}" height="5" fill="{BLUE}"/><rect x="{sp}" width="{self.w - sp}" height="5" fill="{RED}"/></g>'
                         f'<rect x=".75" y=".75" width="{self.w - 1.5}" height="{self.h - 1.5}" rx="{rx}" fill="none" stroke="url(#{p}-edge)" stroke-width="1.5"/>')
    def slashes(self, x, y):
        return ''.join(f'<path d="M{x + i * 13 + 7} {y} h8 l-7 20 h-8z" fill="{RED}"/>' for i in range(3))
    def header(self, num, label):
        return self.t(56, 58, num, 'm5', 13, BLUE, ls=.14) + self.t(86, 58, '/  ' + label, 'm5', 13, MUTED, ls=.14)
    def render(self):
        faces = [f"@font-face{{font-family:{FONT_FILES[k][1]};font-weight:{FONT_FILES[k][2]};font-style:normal;font-display:block;"
                 f"src:url(data:font/woff2;base64,{woff2_b64(k, c)}) format('woff2')}}" for k, c in sorted(self.used.items())]
        p = self.p
        css = '\n'.join(faces + self.css) + (f'\n.{p}-still{{display:none}}\n@media (prefers-reduced-motion: reduce){{'
                                             f'*{{animation:none!important;transition:none!important}} .{p}-live{{display:none}} .{p}-still{{display:inline}}}}')
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" viewBox="0 0 {self.w} {self.h}" '
                f'role="img" aria-labelledby="{p}-title {p}-desc" fill="none">\n<title id="{p}-title">{escape(self.title)}</title>'
                f'<desc id="{p}-desc">{escape(self.desc)}</desc>\n<style>\n{css}\n</style>\n<defs>\n' + '\n'.join(self.defs)
                + '\n</defs>\n' + '\n'.join(self.body) + '\n</svg>\n')

# ------------------------------------------------------------------ stack.svg
def ellipse_pts(cx, cy, rx, ry, rot, n=2000):
    ph = math.radians(rot)
    return [(cx + rx * math.cos(t) * math.cos(ph) - ry * math.sin(t) * math.sin(ph),
             cy + rx * math.cos(t) * math.sin(ph) + ry * math.sin(t) * math.cos(ph))
            for t in (2 * math.pi * k / n for k in range(n + 1))]

def build_stack(groups, highlight, orbits):
    S = SVG('s', 1000, 700, 'Tech stack', '; '.join(f'{g["label"].title()}: ' + ', '.join(g['items']) for g in groups))
    S.css.append(rise_css('s')); B = S.body
    cx, cy = 500, 352
    fs, ch, padx, gap, top = 13.5, 32, 12, 8, 596
    cols = [(56, 488), (512, 944)]
    def layout(gs, x0, x1, y):
        out = []
        for gi, g in gs:
            out.append(('label', x0, y, g['label'], gi)); y += 14; x = x0
            for it in g['items']:
                w = tw('m5', it, fs) + padx * 2
                if x + w > x1: x = x0; y += ch + gap
                out.append(('chip', x, y, it, w, gi)); x += w + gap
            y += ch + 34
        return out, y
    idx = list(enumerate(groups)); best = None
    for split in range(1, len(groups)):
        l, yl = layout(idx[:split], *cols[0], top); r, yr = layout(idx[split:], *cols[1], top)
        if best is None or max(yl, yr) < best[0]: best = (max(yl, yr), l + r)
    ybot, chips = best
    S.h = round(ybot - 4)
    S.background(glows=[(cx, cy, 380, 'b'), (900, S.h - 80, 280, 'r'), (120, 200, 220, 'r')])
    B.append(f'<g class="s-fade" {d(0)}>' + S.header('03', 'STACK') + S.slashes(900, 40) + '</g>')
    B.append(f'<g class="s-rise" {d(.1)}>' + S.t(54, 126, 'TOOLS I SHIP WITH.', 'd8', 62, WHITE) + '</g>')
    B.append(f'<line x1="56" y1="{top - 38}" x2="944" y2="{top - 38}" stroke="{LINE}"/>')
    k = 0; out = ''
    for c in chips:
        if c[0] == 'label':
            _, x, yy, label, gi = c
            out += f'<g class="s-fade" {d(.3 + gi * .1)}>' + S.t(x, yy, label, 'm5', 12, BLUE if gi % 2 == 0 else RED, ls=.16) + '</g>'
        else:
            _, x, yy, it, w, gi = c
            hl = it in highlight
            out += (f'<g class="s-rise" {d(.4 + k * .025)}><rect x="{x:.1f}" y="{yy + 6}" width="{w:.1f}" height="{ch}" rx="10" '
                    f'fill="{"#13224a" if hl else PANEL}" stroke="{"#2f5fb8" if hl else "#22304f"}"/>'
                    + S.t(x + padx, yy + 27, it, 'm5', fs, WHITE if hl else SOFT) + '</g>'); k += 1
    B.append(out)
    S.defs.append(f'<radialGradient id="s-core" cx=".5" cy=".4" r=".6"><stop offset="0" stop-color="#1d3a7a"/><stop offset="1" stop-color="#0b1226"/></radialGradient>'
                  f'<linearGradient id="s-oring" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{BLUE}" stop-opacity=".7"/><stop offset="1" stop-color="{RED}" stop-opacity=".7"/></linearGradient>')
    geo = [(182, 78, -7, 26, 1), (300, 118, 5, 38, 0), (412, 156, -3, 52, 1)]
    rings = [(g, o) for g, o in zip(geo, orbits) if o]
    for slug in sorted({s for _, o in rings for s in o}):
        S.defs.append(f'<symbol id="s-i-{slug}" viewBox="0 0 24 24"><path d="{A.icon(slug)}"/></symbol>')
    tables = []
    for (rx, ry, rot, *_), _o in rings:
        pts = ellipse_pts(cx, cy, rx, ry, rot); L = [0.0]
        for q in range(1, len(pts)): L.append(L[-1] + math.dist(pts[q - 1], pts[q]))
        tables.append((pts, L))
    def positions(phs):
        out = []
        for (pts, L), (_, o), ph in zip(tables, rings, phs):
            for j in range(len(o)):
                out.append(pts[min(bisect.bisect_left(L, L[-1] * ((j / len(o) + ph) % 1)), len(pts) - 1)])
        return out
    def penalty(phs):
        ps = positions(phs)
        return (sum(max(0, 92 - math.dist(a, b)) ** 2 for a, b in itertools.combinations(ps, 2))
                + sum(max(0, 100 - math.dist(p, (cx, cy))) ** 2 * 2 for p in ps))
    grid = [q / 48 for q in range(48)]
    choices = [grid[::4]] + [grid[::2]] * (len(rings) - 1)
    PH = min(itertools.product(*choices), key=penalty) if rings else ()
    orb, live, still = '', '', ''
    for oi, (((rx, ry, rot, dur, sweep), slugs), (pts, L), ph) in enumerate(zip(rings, tables, PH)):
        a0, a1 = pts[0], pts[1000]
        orb += (f'<path d="M{a0[0]:.2f} {a0[1]:.2f} A{rx} {ry} {rot} 1 1 {a1[0]:.2f} {a1[1]:.2f} A{rx} {ry} {rot} 1 1 {a0[0]:.2f} {a0[1]:.2f}Z" '
                f'stroke="url(#s-oring)" stroke-opacity="{.55 - oi * .1:.2f}" stroke-width="1.3" stroke-dasharray="{3 + oi} {6 + oi * 2}"/>')
        for j, slug in enumerate(slugs):
            kk = min(bisect.bisect_left(L, L[-1] * ((j / len(slugs) + ph) % 1)), 1999)
            sx, sy = pts[kk]; ox, oy = pts[(kk + 1000) % 2000]
            mp = f'M0 0 A{rx} {ry} {rot} 1 {sweep} {ox - sx:.2f} {oy - sy:.2f} A{rx} {ry} {rot} 1 {sweep} 0 0'
            node = (f'<circle r="23" fill="#0d1630" stroke="#2c3d66"/><circle r="23" fill="url(#s-gb)" opacity=".5"/>'
                    f'<use href="#s-i-{slug}" x="-12" y="-12" width="24" height="24" fill="{icon_color(slug)}"/>'
                    + S.t(0, 40, ICON_LABEL.get(slug, slug.replace('dot', '.').title()), 'm5', 11.5, SOFT, 'middle'))
            live += (f'<g transform="translate({sx:.2f} {sy:.2f})"><g><animateMotion path="{mp}" dur="{dur}s" begin="0s" '
                     f'repeatCount="indefinite" calcMode="paced"/>{node}</g></g>')
            still += f'<g transform="translate({sx:.2f} {sy:.2f})">{node}</g>'
    core = (f'<circle cx="{cx}" cy="{cy}" r="50" fill="url(#s-core)" stroke="url(#s-oring)" stroke-width="2"/>'
            + S.t(cx, cy + 8, 'AKS', 'd8', 36, WHITE, 'middle', .04) + S.t(cx, cy + 28, 'STACK', 'm5', 10, SOFT, 'middle', .2))
    S.css.append('@keyframes s-pulse{0%,100%{opacity:.35;transform:scale(1)}50%{opacity:.8;transform:scale(1.08)}}'
                 f'.s-pulse{{transform-box:view-box;transform-origin:{cx}px {cy}px;animation:s-pulse 4s ease-in-out infinite}}')
    B.append(f'<g class="s-fade" {d(.2)}>{orb}<circle class="s-pulse" cx="{cx}" cy="{cy}" r="62" stroke="{BLUE}" stroke-opacity=".5"/>{core}'
             f'<g class="s-live">{live}</g><g class="s-still">{still}</g></g>')
    return S

# ------------------------------------------------------------------ latest-repos.svg
def build_latest(user, cards, total, latest_push):
    S = SVG('l', 1000, 600, 'Latest on GitHub',
            'Most recently updated repositories: ' + '; '.join(f'{c["title"]} ({", ".join(c["stack"][:5])})' for c in cards))
    S.css.append(rise_css('l'))
    S.css.append('@keyframes l-ping{0%{transform:scale(1);opacity:.8}100%{transform:scale(2.8);opacity:0}}'
                 '.l-ping{transform-box:fill-box;transform-origin:center;animation:l-ping 2s ease-out infinite}'
                 '@keyframes l-scan{0%{transform:translateX(-260px)}100%{transform:translateX(1000px)}}'
                 '.l-scan{animation:l-scan 4.5s cubic-bezier(.45,0,.2,1) 1.2s infinite both}')
    cw, chh, gx, gy, top = 434, 192, 20, 20, 196
    rows = max(1, math.ceil(len(cards) / 2))
    S.h = top + rows * (chh + gy) - gy + 44
    S.background(glows=[(200, 160, 300, 'b'), (860, S.h - 120, 300, 'r')])
    B = S.body
    B.append(f'<g class="l-fade" {d(0)}>' + S.header('08', 'LATEST ON GITHUB') + S.slashes(900, 40) + '</g>')
    B.append(f'<g class="l-rise" {d(.1)}>' + S.t(54, 126, 'FRESH FROM MY GITHUB.', 'd8', 62, WHITE) + '</g>')
    sub = f'Auto-synced from github.com/{user} · {total} public projects · last push {latest_push}'
    B.append(f'<g class="l-rise" {d(.2)}>' + S.t(56, 156, sub, 'b4', fit('b4', sub, 16, 700), MUTED) + '</g>')
    pill_w = tw('m5', 'AUTO-SYNC', 11, .14) + 40
    B.append(f'<g class="l-fade" {d(.3)}><rect x="{944 - pill_w:.1f}" y="134" width="{pill_w:.1f}" height="28" rx="14" fill="#10281c" stroke="#2e8b57"/>'
             f'<circle class="l-ping" cx="{944 - pill_w + 16:.1f}" cy="148" r="4" fill="{GREEN}"/><circle cx="{944 - pill_w + 16:.1f}" cy="148" r="4" fill="{GREEN}"/>'
             + S.t(944 - pill_w + 28, 152.5, 'AUTO-SYNC', 'm5', 11, GREEN, ls=.14) + '</g>')
    S.defs.append(f'<linearGradient id="l-sweep" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{BLUE}" stop-opacity="0"/>'
                  f'<stop offset=".5" stop-color="{BLUE}"/><stop offset="1" stop-color="{BLUE}" stop-opacity="0"/></linearGradient>'
                  f'<clipPath id="l-line"><rect x="56" y="{top - 22}" width="888" height="2"/></clipPath>'
                  f'<linearGradient id="l-cf" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#121d38"/><stop offset="1" stop-color="#0a1122"/></linearGradient>')
    B.append(f'<rect x="56" y="{top - 22}" width="888" height="1" fill="{LINE}"/>'
             f'<g clip-path="url(#l-line)"><g class="l-scan"><rect x="0" y="{top - 22}" width="240" height="2" fill="url(#l-sweep)"/></g></g>')
    if not cards:
        B.append(S.t(500, top + 60, 'No public repositories yet.', 'b4', 18, MUTED, 'middle')); return S
    book = '<path d="M-6 -7 H5 a1.5 1.5 0 0 1 1.5 1.5 V7 H-4.5 a1.5 1.5 0 0 1 -1.5 -1.5 Z M-6 4 a1.6 1.6 0 0 1 1.5 -1.4 H6.5" stroke="#8a95ad" stroke-width="1.5" fill="none"/>'
    for k, c in enumerate(cards):
        x = 56 + (k % 2) * (cw + gx); y = top + (k // 2) * (chh + gy)
        acc = BLUE if k % 2 == 0 else RED
        body = (f'<rect x="{x}" y="{y}" width="{cw}" height="{chh}" rx="18" fill="url(#l-cf)" stroke="#24345a"/>'
                f'<rect x="{x + 20}" y="{y}" width="{cw - 40}" height="3" rx="1.5" fill="{acc}" opacity=".85"/>'
                f'<g transform="translate({x + 28} {y + 36})">{book}</g>')
        right = x + cw - 18
        if c['homepage']:
            pw = tw('m5', 'LIVE', 9.5, .1) + 16
            body += (f'<rect x="{right - pw:.1f}" y="{y + 26}" width="{pw:.1f}" height="18" rx="9" fill="#13301f" stroke="#2e8b57"/>'
                     + S.t(right - pw / 2, y + 38.8, 'LIVE', 'm5', 9.5, GREEN, 'middle', .1)); right -= pw + 8
        body += S.t(x + 44, y + 43, c['title'], 'd6', fit('d6', c['title'], 26, right - x - 52, .02), WHITE, ls=.02)
        body += S.t(x + 22, y + 66, '/' + c['name'], 'm5', fit('m5', '/' + c['name'], 11.5, cw - 44), acc)
        for j, line in enumerate(wrap('b4', c['description'], 15, cw - 44, 2)):
            body += S.t(x + 22, y + 92 + j * 20, line, 'b4', 15, SOFT)
        cxp, cyp = x + 22, y + 124
        shown = 0
        for t in c['stack']:
            w = tw('m5', t, 10.5) + 16
            rest = len(c['stack']) - shown - 1
            if cxp + w + (40 if rest else 0) > x + cw - 22: break
            body += (f'<rect x="{cxp:.1f}" y="{cyp}" width="{w:.1f}" height="22" rx="7" fill="{PANEL2}" stroke="#26345a"/>'
                     + S.t(cxp + 8, cyp + 15, t, 'm5', 10.5, SOFT)); cxp += w + 6; shown += 1
        if shown < len(c['stack']):
            body += S.t(cxp + 2, cyp + 15, f'+{len(c["stack"]) - shown}', 'm5', 10.5, MUTED)
        fy = y + chh - 20
        if c['language']:
            body += (f'<circle cx="{x + 27}" cy="{fy - 4}" r="5" fill="{LANG_COLOR.get(c["language"], MUTED)}"/>'
                     + S.t(x + 38, fy, c['language'], 'b4', 13, MUTED))
        n = c['commits']
        meta = f'{n} commit{"" if n == 1 else "s"}  ·  {c["updated"]}' + (f'  ·  ★ {c["stars"]}' if c['stars'] else '')
        body += S.t(x + cw - 22, fy, meta, 'm5', 11, MUTED, 'end')
        B.append(f'<g class="l-rise" {d(.35 + k * .12)}>{body}</g>')
    return S

# ------------------------------------------------------------------ commit-activity.svg
def bar_path(x, y, w, h, r=4):
    """Horizontal bar: square at the baseline (left), rounded data end (right)."""
    r = min(r, w / 2, h / 2)
    return (f'M{x:.1f} {y:.1f}H{x + w - r:.1f}A{r} {r} 0 0 1 {x + w:.1f} {y + r:.1f}V{y + h - r:.1f}'
            f'A{r} {r} 0 0 1 {x + w - r:.1f} {y + h:.1f}H{x:.1f}Z')

def col_path(x, base, w, h, r=3):
    """Vertical column: square at the baseline (bottom), rounded top."""
    r = min(r, w / 2, h)
    return (f'M{x:.1f} {base:.1f}V{base - h + r:.1f}A{r} {r} 0 0 1 {x + r:.1f} {base - h:.1f}H{x + w - r:.1f}'
            f'A{r} {r} 0 0 1 {x + w:.1f} {base - h + r:.1f}V{base:.1f}Z')

def count_up(S, x, y, value, font, size, fill, begin=.6, dur=1.3, frames=16):
    """Number that counts up from 0 (SMIL, discrete frames). Renderers without SMIL and
    reduced-motion viewers get the final number only."""
    vals = []
    for f in range(1, frames + 1):
        v = round(value * (1 - (1 - f / frames) ** 3))
        if not vals or v != vals[-1]: vals.append(v)
    T = begin + dur; live = ''
    for i, v in enumerate(vals):
        t0, t1 = (begin + dur * i / len(vals)) / T, (begin + dur * (i + 1) / len(vals)) / T
        last = i == len(vals) - 1
        anim = (f'<animate attributeName="opacity" values="0;1" keyTimes="0;{t0:.4f}" dur="{T:.2f}s" calcMode="discrete" fill="freeze"/>'
                if last else f'<animate attributeName="opacity" values="0;1;0" keyTimes="0;{t0:.4f};{t1:.4f}" dur="{T:.2f}s" calcMode="discrete" fill="freeze"/>')
        live += S.t(x, y, f'{v:,}', font, size, fill).replace('<text ', f'<text opacity="{1 if last else 0}" ', 1).replace('</text>', anim + '</text>')
    return f'<g class="c-live">{live}</g><g class="c-still">' + S.t(x, y, f'{value:,}', font, size, fill) + '</g>'

def build_commits(user, stats, max_rows=8):
    """stats: list of dict(name, title, total, weekly[WEEKS], last) for every scanned repo."""
    rows = sorted(stats, key=lambda s: (-s['total'], s['title'].lower()))[:max_rows]
    total_all = sum(s['total'] for s in stats)
    weekly_all = [sum(s['weekly'][i] for s in stats) for i in range(WEEKS)]
    recent_all = sum(weekly_all)
    month = lambda s: sum(s['weekly'][-4:])
    top = max(stats, key=lambda s: (month(s), sum(s['weekly']), s['last'] or datetime.min.replace(tzinfo=timezone.utc)), default=None)
    fmt = lambda dt: dt.strftime('%d %b %Y').lstrip('0') if dt else '—'
    S = SVG('c', 1000, 800, 'Commit activity by repository',
            f'{total_all} commits across {len(stats)} repositories; {recent_all} in the last {WEEKS} weeks. '
            + '; '.join(f'{s["title"]}: {s["total"]} commits, {sum(s["weekly"])} in the last {WEEKS} weeks, '
                        f'last commit {fmt(s["last"])}' for s in rows))
    S.css.append(rise_css('c'))
    S.css.append('@keyframes c-grow{from{transform:scaleX(0)}to{transform:none}}'
                 '.c-grow{transform-box:fill-box;transform-origin:left center;animation:c-grow 1.1s cubic-bezier(.2,.8,.2,1) both}'
                 '@keyframes c-up{from{transform:scaleY(0)}to{transform:none}}'
                 '.c-up{transform-box:fill-box;transform-origin:center bottom;animation:c-up .7s cubic-bezier(.2,.8,.2,1) both}'
                 '@keyframes c-glint{0%{transform:translateX(0)}35%,100%{transform:translateX(560px)}}'
                 '.c-glint{animation:c-glint 6s cubic-bezier(.45,0,.25,1) infinite both}'
                 '@keyframes c-ping{0%{transform:scale(1);opacity:.85}100%{transform:scale(3);opacity:0}}'
                 '.c-ping{transform-box:fill-box;transform-origin:center;animation:c-ping 1.8s ease-out infinite}'
                 '@keyframes c-beat{0%,100%{opacity:1}50%{opacity:.55}}'
                 '.c-beat{animation:c-beat 2.2s ease-in-out 2s infinite}'
                 '@keyframes c-tip{from{opacity:0;transform:translateX(-8px)}to{opacity:1;transform:none}}'
                 '.c-tip{animation:c-tip .5s ease-out both}')
    S.defs.append(f'<linearGradient id="c-shine" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#fff" stop-opacity="0"/>'
                  f'<stop offset=".5" stop-color="#fff" stop-opacity=".45"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>')
    B = S.body
    DIM = '#2f4b86'  # de-emphasised weeks; the current week wears the accent
    # ---- stat tiles
    ty, th, tw_, tg = 186, 108, (888 - 40) / 3, 20
    ws = max(weekly_all) or 1
    active_weeks = sum(1 for v in weekly_all if v)
    tiles = [('Total commits', f'{total_all:,}', f'across {len(stats)} public repositories', BLUE),
             (f'Last {WEEKS} weeks', f'{recent_all:,}', f'{active_weeks} of {WEEKS} weeks with commits', RED),
             ('Most active, last 4 weeks' if top and month(top) else f'Most active, last {WEEKS} weeks' if recent_all else 'Most recent work',
              top['title'] if top else '—',
              (f'{month(top)} commits in 4 weeks' if month(top) else f'{sum(top["weekly"])} commits in {WEEKS} weeks' if recent_all
               else f'last commit {fmt(top["last"])}') if top else '', BLUE)]
    for k, (label, value, sub, acc) in enumerate(tiles):
        x = 56 + k * (tw_ + tg)
        vsize = 46 if k < 2 else fit('d6', value, 32, tw_ - 44)
        t = (f'<rect x="{x:.1f}" y="{ty}" width="{tw_:.1f}" height="{th}" rx="16" fill="{PANEL}" stroke="#22304f"/>'
             f'<rect x="{x:.1f}" y="{ty + 18}" width="3" height="28" rx="1.5" fill="{acc}"/>'
             + S.t(x + 22, ty + 32, label, 'b4', 14, MUTED)
             + (count_up(S, x + 21, ty + 74, [total_all, recent_all][k], 'd8', vsize, WHITE, begin=.5 + k * .15) if k < 2
                else S.t(x + 21, ty + 70, value, 'd6', vsize, WHITE))
             + S.t(x + 22, ty + 95, sub, 'b4', fit('b4', sub, 13, tw_ - 44), SOFT))
        if k == 2 and top and top['weekly'][-1]:
            px = x + tw_ - 26
            t += (f'<circle class="c-ping" cx="{px:.1f}" cy="{ty + 28}" r="4.5" fill="{GREEN}"/>'
                  f'<circle cx="{px:.1f}" cy="{ty + 28}" r="4.5" fill="{GREEN}"/>')
        if k == 1:  # 12-week trend sparkline for all repos together
            sx, base, cwid, gap = x + tw_ - 22 - (WEEKS * 7 + (WEEKS - 1) * 3), ty + 72, 7, 3
            for i, v in enumerate(weekly_all):
                if v:
                    t += f'<path class="c-up" style="animation-delay:{.6 + i * .04:.2f}s" d="{col_path(sx + i * (cwid + gap), base, cwid, max(2, v / ws * 30), 2)}" fill="{BLUE if i == WEEKS - 1 else DIM}"/>'
            t += f'<line x1="{sx:.1f}" y1="{base + .5}" x2="{sx + WEEKS * (cwid + gap) - gap:.1f}" y2="{base + .5}" stroke="{LINE}"/>'
        B.append(f'<g class="c-rise" {d(.25 + k * .1)}>{t}</g>')
    # ---- table
    NX, BX, BW, WX = 56, 330, 300, 700
    cw_, cg = 14, (944 - 700 - WEEKS * 14) / (WEEKS - 1)
    hy = ty + th + 44
    B.append(f'<g class="c-fade" {d(.5)}>' + S.t(NX, hy, 'REPOSITORY', 'm5', 11, MUTED, ls=.14)
             + S.t(BX, hy, 'TOTAL COMMITS', 'm5', 11, MUTED, ls=.14) + S.t(WX, hy, f'LAST {WEEKS} WEEKS', 'm5', 11, MUTED, ls=.14) + '</g>')
    y0, rh = hy + 14, 62
    tmax = max([s['total'] for s in rows] + [1])
    wmax = max([v for s in rows for v in s['weekly']] + [1])
    if not rows:
        B.append(S.t(500, y0 + 40, 'No commits yet.', 'b4', 16, MUTED, 'middle'))
    for k, s in enumerate(rows):
        y = y0 + k * rh; mid = y + rh / 2
        row = f'<line x1="{NX}" y1="{y:.1f}" x2="944" y2="{y:.1f}" stroke="{LINE}"/>'
        tsz = fit('d6', s['title'], 22, BX - NX - 44, .02)
        row += S.t(NX, mid - 3, s['title'], 'd6', tsz, WHITE, ls=.02)
        if s['weekly'][-1]:  # committed to this week
            px = NX + tw('d6', s['title'], tsz, .02) + 12
            row += (f'<circle class="c-ping" cx="{px:.1f}" cy="{mid - 10:.1f}" r="3.5" fill="{GREEN}"/>'
                    f'<circle cx="{px:.1f}" cy="{mid - 10:.1f}" r="3.5" fill="{GREEN}"/>')
        sub = f'/{s["name"]} · last commit {fmt(s["last"])}'
        row += S.t(NX, mid + 16, sub, 'm5', fit('m5', sub, 11, BX - NX - 24), MUTED)
        if s['total']:
            w = max(6, s['total'] / tmax * BW)
            bp = bar_path(BX, mid - 8, w, 16)
            S.defs.append(f'<clipPath id="c-bar{k}"><path d="{bp}"/></clipPath>')
            row += (f'<path class="c-grow" style="animation-delay:{.7 + k * .08:.2f}s" d="{bp}" fill="{BLUE}"/>'
                    f'<g clip-path="url(#c-bar{k})"><g class="c-glint" style="animation-delay:{2.2 + k * .35:.2f}s">'
                    f'<rect x="{BX - 140}" y="{mid - 8:.1f}" width="110" height="16" fill="url(#c-shine)" transform="skewX(-20)"/></g></g>')
            row += f'<g class="c-tip" style="animation-delay:{1.5 + k * .08:.2f}s">' + S.t(BX + w + 10, mid + 5, f'{s["total"]:,}', 'm5', 13, WHITE) + '</g>'
        base = mid + 15
        row += f'<line x1="{WX}" y1="{base + .5:.1f}" x2="944" y2="{base + .5:.1f}" stroke="{LINE}"/>'
        for i, v in enumerate(s['weekly']):
            if v:
                cur = i == WEEKS - 1
                col = (f'<path class="c-up" style="animation-delay:{.9 + k * .08 + i * .03:.2f}s" '
                       f'd="{col_path(WX + i * (cw_ + cg), base, cw_, max(3, v / wmax * 30))}" fill="{BLUE if cur else DIM}"/>')
                row += f'<g class="c-beat">{col}</g>' if cur else col
        B.append(f'<g class="c-fade" {d(.55 + k * .08)}>{row}</g>')
    yb = y0 + max(1, len(rows)) * rh
    B.append(f'<line x1="{NX}" y1="{yb:.1f}" x2="944" y2="{yb:.1f}" stroke="{LINE}"/>'
             + S.t(WX, yb + 18, f'{WEEKS} wks ago', 'm5', 10, MUTED) + S.t(944, yb + 18, 'this week', 'm5', 10, MUTED, 'end'))
    note = (f'Weekly columns share one scale (busiest week: {wmax} commits); the current week is bright blue, '
            'and a green dot marks repos with commits this week. Counts cover each repo\'s main branch.')
    B.append(S.t(NX, yb + 48, note, 'b4', fit('b4', note, 13, 888), MUTED))
    S.h = round(yb + 72)
    S.background(glows=[(180, 230, 280, 'b'), (860, S.h - 140, 300, 'r')])
    S.body.insert(0, S.body.pop())  # background goes underneath everything
    B.insert(1, f'<g class="c-fade" {d(0)}>' + S.header('09', 'COMMIT ACTIVITY') + S.slashes(900, 40) + '</g>'
             f'<g class="c-rise" {d(.1)}>' + S.t(54, 126, 'WHERE MY COMMITS GO.', 'd8', 62, WHITE) + '</g>'
             f'<g class="c-rise" {d(.2)}>' + S.t(56, 156, f'Commits per repository · auto-synced from github.com/{user}', 'b4', 16, MUTED) + '</g>')
    return S

# ------------------------------------------------------------------ shared project model
def count_tests(src, repo):
    """Automated tests in the repo: Python test functions + JS it()/test() calls in test files."""
    n = 0
    for p, s in src.tree(repo):
        low = p.lower(); base = low.rsplit('/', 1)[-1]
        if s > 400_000 or set(low.split('/')[:-1]) & SKIP_DIRS: continue
        if low.endswith('.py') and (base.startswith('test') or base.endswith('_test.py') or '/tests/' in '/' + low):
            n += len(re.findall(r'^\s*(?:async\s+)?def test_\w+', src.read(repo, p), re.M))
        elif re.search(r'\.(test|spec)\.(js|jsx|mjs)$', low):
            n += len(re.findall(r'\b(?:it|test)\(\s*[\'"`]', src.read(repo, p)))
    return n

def lang_dot(p):
    if p.get('language') in LANG_COLOR: return LANG_COLOR[p['language']]
    st = p.get('stack', [])
    if 'Python' in st or {'Django', 'Flask', 'FastAPI', 'Pandas'} & set(st): return LANG_COLOR['Python']
    return LANG_COLOR['JavaScript']

def embedded_png(path):
    """Re-use the photo already embedded in an SVG we generated earlier."""
    try:
        m = re.search(r'href="(data:image/png;base64,[A-Za-z0-9+/=]+)"', open(path, encoding='utf-8').read())
        return m.group(1) if m else None
    except OSError:
        return None

GREEN_BG, GREEN_ST = '#13301f', '#2e8b57'

def chips_row(S, x, y, items, fs=11, h=22, gap=6, maxw=9999, hl=()):
    out, cx, cy = '', x, y
    for it in items:
        w = tw('m5', it, fs) + 16
        if cx + w > x + maxw: cx = x; cy += h + gap
        strong = it in hl
        out += (f'<rect x="{cx:.1f}" y="{cy}" width="{w:.1f}" height="{h}" rx="7" fill="{"#13224a" if strong else PANEL2}" '
                f'stroke="{"#2f5fb8" if strong else "#26345a"}"/>' + S.t(cx + 8, cy + h * .68, it, 'm5', fs, WHITE if strong else SOFT))
        cx += w + gap
    return out, cy + h

def fit_chips(items, fs, maxw, rows=1, gap=6):
    """Keep as many chips as fit in `rows` rows."""
    out, cx, r = [], 0, 1
    for it in items:
        w = tw('m5', it, fs) + 16
        if cx + w > maxw:
            if r == rows: break
            r += 1; cx = 0
        out.append(it); cx += w + gap
    return out

def pill(S, x, y, text, live=True):
    w = tw('m5', text, 9.5, .1) + 16
    fill, stroke, ink = (GREEN_BG, GREEN_ST, GREEN) if live else ('#141d36', '#2c3b63', SOFT)
    return (f'<rect x="{x - w:.1f}" y="{y}" width="{w:.1f}" height="18" rx="9" fill="{fill}" stroke="{stroke}"/>'
            + S.t(x - w / 2, y + 12.8, text, 'm5', 9.5, ink, 'middle', .1))

# ------------------------------------------------------------------ projects.svg (photo + featured cards)
def build_projects(projects, featured, photo, total_commits):
    S = SVG('p', 1000, 1000, 'Projects',
            'Aditya presenting his projects with open arms. Featured: ' + '; '.join(f'{f["title"]} ({", ".join(f["chips"][:5])})' for f in featured)
            + '. More: ' + ', '.join(q['title'] for q in projects if q['key'] not in {f['key'] for f in featured}) + '. Clickable links are below this image.')
    p = 'p'; W = 1000
    S.css.append(rise_css(p))
    S.css.append('''@keyframes p-bob{0%,100%{transform:translateY(0)}50%{transform:translateY(-5px)}}
.p-bob{animation:p-bob 6s ease-in-out 1.4s infinite}
@keyframes p-hover{0%,100%{transform:translateY(0)}50%{transform:translateY(-8px)}}
.p-hover{animation:p-hover 5s ease-in-out 1.6s infinite}.p-hover2{animation:p-hover 5s ease-in-out 4.1s infinite}
@keyframes p-glow{0%,100%{opacity:.55;transform:scale(.9)}50%{opacity:1;transform:scale(1.15)}}
.p-glow{transform-box:view-box;animation:p-glow 2.6s ease-in-out infinite}
@keyframes p-spark{0%{opacity:0;transform:translateY(0)}20%{opacity:1}100%{opacity:0;transform:translateY(-54px)}}
.p-spark{animation:p-spark 3s ease-out infinite both;opacity:0}
@keyframes p-pop{from{opacity:0;transform:scale(.55)}to{opacity:1;transform:none}}
.p-pop{transform-box:view-box;animation:p-pop .9s cubic-bezier(.2,.9,.25,1.15) both}''')
    S.defs.append(f'<radialGradient id="p-palm" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="#9cc2ff" stop-opacity=".95"/>'
                  f'<stop offset=".45" stop-color="{BLUE}" stop-opacity=".45"/><stop offset="1" stop-color="{BLUE}" stop-opacity="0"/></radialGradient>'
                  f'<linearGradient id="p-cardf" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#142142"/><stop offset="1" stop-color="#0a1123"/></linearGradient>'
                  f'<linearGradient id="p-edge2" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{BLUE}"/><stop offset="1" stop-color="{RED}"/></linearGradient>')
    def palm_fx(x, y, dl0=0.0):
        out = (f'<g class="p-glow" style="transform-origin:{x:.1f}px {y:.1f}px;animation-delay:{dl0:.2f}s">'
               f'<ellipse cx="{x:.1f}" cy="{y:.1f}" rx="46" ry="22" fill="url(#p-palm)"/></g>')
        for dx, r, dl, col in [(-18, 2.6, 0, BLUE), (6, 2, .8, WHITE), (20, 2.8, 1.6, RED), (-4, 1.8, 2.3, BLUE)]:
            out += f'<g class="p-spark" style="animation-delay:{dl0 + dl:.2f}s"><circle cx="{x + dx:.1f}" cy="{y - 6:.1f}" r="{r}" fill="{col}"/></g>'
        return out
    def card(f, w, accent):
        body, cy = '', 30
        body += S.t(20, cy, f['tag'], 'm5', 10.5, accent, ls=.14) + pill(S, w - 18, cy - 13, 'LIVE' if f['live'] else 'CODE', f['live'])
        cy += 38
        body += S.t(18, cy, f['title'].upper(), 'd8', fit('d8', f['title'].upper(), 34, w - 40, .01), WHITE, ls=.01)
        cy += 22
        body += S.t(20, cy, f['subtitle'], 'b4', fit('b4', f['subtitle'], 15, w - 40), MUTED)
        cy += 14
        if f['points']:
            fs = min(fit('b4', pt, 14.5, w - 56) for pt in f['points'])
            for pt in f['points']:
                cy += 23
                body += f'<rect x="22" y="{cy - 8}" width="6" height="6" rx="1.5" fill="{accent}"/>' + S.t(36, cy, pt, 'b4', fs, SOFT)
        else:
            for ln in wrap('b4', f['description'], 14.5, w - 40, 3):
                cy += 21; body += S.t(20, cy, ln, 'b4', 14.5, SOFT)
        cy += 16
        ch, cb = chips_row(S, 20, cy, fit_chips(f['chips'], 10.5, w - 40, 2), 10.5, 22, 6, w - 40, tuple(f['chips'][:2]))
        h = cb + 18
        return (f'<rect width="{w}" height="{h:.0f}" rx="20" fill="url(#p-cardf)" stroke="url(#p-edge2)" stroke-width="1.4"/>'
                f'<rect x="20" width="{w - 40}" height="3" rx="1.5" fill="{accent}"/>' + body + ch), h
    IW = 660; sc = IW / 1536; IH = 960 * sc; IX = (W - IW) / 2; IY = 272
    lp = [IX + 106 * sc, IY + 544 * sc]; rp = [IX + 1466 * sc, IY + 507 * sc]
    ltop, rtop = IY + 468 * sc, IY + 423 * sc
    cw = 316
    cards = [card(f, cw, acc) for f, acc in zip(featured, (BLUE, RED))]
    x1 = max(56, min(lp[0] - cw / 2, 400)); x2 = min(W - 56 - cw, rp[0] - cw / 2)
    ys = [ltop - 22 - cards[0][1] if cards else ltop, rtop - 22 - cards[1][1] if len(cards) > 1 else rtop]
    if min(ys) < 186:
        dy = 186 - min(ys); IY += dy; lp[1] += dy; rp[1] += dy; ys = [y + dy for y in ys]
    panel_y = IY + IH - 46
    more = [q for q in projects if q['key'] not in {f['key'] for f in featured}][:4]
    gx0, gx1 = 56, W - 56
    mw, mh = (gx1 - gx0 - 48) / 2, 118
    grid_y = panel_y + 62
    rows = max(1, math.ceil(len(more) / 2))
    S.h = round(grid_y + rows * (mh + 16) + 30)
    S.background(glows=[(500, IY + 200, 360, 'b'), (180, 300, 220, 'r'), (860, 260, 240, 'r')])
    B = S.body
    n, live = len(projects), sum(1 for q in projects if q['live'])
    B.append(f'<g class="p-fade" {d(0)}>' + S.header('06', 'PROJECTS') + S.slashes(900, 40) + '</g>')
    B.append(f'<g class="p-rise" {d(.1)}>' + S.t(54, 126, "WORK I'VE SHIPPED.", 'd8', 62, WHITE) + '</g>')
    sub = f'{n} projects · {live} live · {total_commits:,} commits on GitHub · auto-synced'
    B.append(f'<g class="p-rise" {d(.2)}>' + S.t(56, 156, sub, 'b4', 16, MUTED) + '</g>')
    S.defs.append(f'<radialGradient id="p-spot" cx=".5" cy=".35" r=".6"><stop offset="0" stop-color="#1d3f8f" stop-opacity=".55"/><stop offset="1" stop-color="{NAVY}" stop-opacity="0"/></radialGradient>'
                  f'<linearGradient id="p-fadeb" x1="0" y1="0" x2="0" y2="1"><stop offset=".78" stop-color="#fff"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>'
                  f'<mask id="p-imgmask" maskContentUnits="userSpaceOnUse"><rect x="{IX - 10}" y="{IY - 10}" width="{IW + 20}" height="{IH + 10}" fill="url(#p-fadeb)"/></mask>'
                  f'<linearGradient id="p-panel" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#0e1730"/><stop offset="1" stop-color="#0a1022"/></linearGradient>')
    B.append(f'<g class="p-fade" {d(.3)}><ellipse cx="500" cy="{IY + 150}" rx="300" ry="230" fill="url(#p-spot)"/>'
             f'<ellipse cx="500" cy="{panel_y + 6}" rx="280" ry="22" fill="{BLUE}" opacity=".12"/></g>')
    B.append(f'<g class="p-rise" {d(.25)}><g class="p-bob"><g mask="url(#p-imgmask)"><image href="{photo}" x="{IX:.1f}" y="{IY:.1f}" width="{IW}" height="{IH:.1f}"/></g>'
             + palm_fx(*lp) + palm_fx(rp[0], rp[1], 1.3) + '</g></g>')
    for k, ((c, _h), (px, py), y) in enumerate(zip(cards, (lp, rp), ys)):
        x = (x1, x2)[k]
        B.append(f'<g class="p-pop" style="transform-origin:{px:.1f}px {py:.1f}px;animation-delay:{.7 + k * .25:.2f}s">'
                 f'<g class="p-hover{"" if k == 0 else "2"}"><g transform="translate({x:.1f} {y:.1f})">{c}</g></g></g>')
    B.append(f'<g class="p-rise" {d(1.1)}><rect x="{gx0 - 16}" y="{panel_y:.1f}" width="{gx1 - gx0 + 32}" height="{S.h - panel_y - 18:.1f}" rx="24" fill="url(#p-panel)" stroke="#26365e"/>'
             f'<rect x="{gx0 - 16}" y="{panel_y:.1f}" width="{gx1 - gx0 + 32}" height="{S.h - panel_y - 18:.1f}" rx="24" fill="url(#p-dots)" opacity=".6"/>'
             + S.t(gx0 + 4, panel_y + 38, 'MORE PROJECTS', 'm5', 12, SOFT, ls=.16) + S.t(gx1 - 4, panel_y + 38, 'auto-synced from GitHub', 'm5', 11, MUTED, 'end') + '</g>')
    for k, m in enumerate(more):
        x = gx0 + 4 + (k % 2) * (mw + 16); y = grid_y + (k // 2) * (mh + 16)
        acc = (BLUE, RED)[(k + k // 2) % 2]
        title = m['title'].upper()
        desc = wrap('b4', m['short'], 14.5, mw - 40, 1)[0]
        body = (f'<rect x="{x:.1f}" y="{y:.1f}" width="{mw:.1f}" height="{mh}" rx="16" fill="{PANEL}" stroke="#22304f"/>'
                f'<rect x="{x:.1f}" y="{y + 18:.1f}" width="3" height="30" rx="1.5" fill="{acc}"/>'
                + S.t(x + 20, y + 38, title, 'd6', fit('d6', title, 24, mw - 110, .02), WHITE, ls=.02)
                + pill(S, x + mw - 16, y + 22, 'LIVE' if m['live'] else 'CODE', m['live'])
                + S.t(x + 20, y + 62, desc, 'b4', 14.5, MUTED))
        ch, _ = chips_row(S, x + 20, y + 78, fit_chips(m['chips'], 10.5, mw - 40, 1), 10.5, 22, 6, mw - 40)
        B.append(f'<g class="p-rise" {d(1.25 + k * .1)}>{body}{ch}</g>')
    return S

# ------------------------------------------------------------------ id-dashboard.svg
def barcode(x, y, w, h, seed):
    bits = ''.join(f'{b:08b}' for b in hashlib.sha256(seed.encode()).digest() * 2)
    out, cx, i = '', x, 0
    while cx < x + w - 3:
        bw = 1 + int(bits[i % len(bits)]) + int(bits[(i + 5) % len(bits)]); gp = 1 + int(bits[(i + 3) % len(bits)])
        if cx + bw > x + w: break
        out += f'<rect x="{cx:.1f}" y="{y}" width="{bw}" height="{h}"/>'; cx += bw + gp; i += 7
    return out

def build_id(user, idc, projects, tests, tested_repos, photo):
    n, live = len(projects), sum(1 for q in projects if q['live'])
    S = SVG('i', 1000, 808, 'Developer ID card and verified stats',
            f'ID card for {idc["name"].title()}: CGPA {idc["cgpa"]}{idc["cgpa_scale"]}, {idc["roles"]} roles, {n} projects ({live} live), '
            f'{tests} automated tests. Projects: ' + ', '.join(q['title'] for q in projects[:6]) + '.')
    S.css.append(rise_css('i'))
    PX, PY = 270, -30
    S.css.append(f'''@keyframes i-drop{{0%{{transform:translateY(-820px)}}62%{{transform:translateY(16px)}}80%{{transform:translateY(-6px)}}100%{{transform:translateY(0)}}}}
.i-drop{{animation:i-drop 1.15s cubic-bezier(.33,.75,.4,1) .15s both}}
@keyframes i-pend{{0%{{transform:rotate(8deg)}}20%{{transform:rotate(-5.6deg)}}40%{{transform:rotate(3.9deg)}}60%{{transform:rotate(-2.8deg)}}80%{{transform:rotate(2.1deg)}}100%{{transform:rotate(-1.7deg)}}}}
.i-pend{{transform-box:view-box;transform-origin:{PX}px {PY}px;animation:i-pend 2.8s ease-in-out .95s both}}
@keyframes i-swing{{0%,100%{{transform:rotate(-1.7deg)}}50%{{transform:rotate(1.7deg)}}}}
.i-swing{{transform-box:view-box;transform-origin:{PX}px {PY}px;animation:i-swing 3.4s ease-in-out 3.75s infinite both}}
@keyframes i-sweep{{0%{{transform:translateX(-460px)}}45%,100%{{transform:translateX(520px)}}}}
.i-sweep{{animation:i-sweep 5.5s ease-in-out 2.2s infinite both}}
@keyframes i-slide{{from{{opacity:0;transform:translateX(26px)}}to{{opacity:1;transform:none}}}}
.i-sr{{animation:i-slide .8s cubic-bezier(.2,.8,.2,1) both}}''')
    S.defs.append(f'''<linearGradient id="i-strap" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#1747a6"/><stop offset=".5" stop-color="{BLUE}"/><stop offset="1" stop-color="#1747a6"/></linearGradient>
<linearGradient id="i-metal" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#f4f6fa"/><stop offset=".45" stop-color="#9aa3b5"/><stop offset=".6" stop-color="#e3e7ee"/><stop offset="1" stop-color="#6c7588"/></linearGradient>
<linearGradient id="i-card" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#13203f"/><stop offset="1" stop-color="#090f20"/></linearGradient>
<linearGradient id="i-foil" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset=".5" stop-color="#fff" stop-opacity=".22"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>
<linearGradient id="i-holo" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{BLUE}"/><stop offset=".5" stop-color="#b9c8ff"/><stop offset="1" stop-color="{RED}"/></linearGradient>
<linearGradient id="i-edge2" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{BLUE}"/><stop offset="1" stop-color="{RED}"/></linearGradient>
<clipPath id="i-cardclip"><rect x="120" y="214" width="300" height="440" rx="24"/></clipPath>
<clipPath id="i-photo"><rect x="170" y="282" width="200" height="200" rx="20"/></clipPath>
<pattern id="i-weave" width="6" height="6" patternUnits="userSpaceOnUse"><path d="M0 6 L6 0" stroke="#fff" stroke-opacity=".10"/></pattern>''')
    sl = lambda x, y, s: ''.join(f'<path d="M{x + i * 13 * s + 7 * s:.1f} {y} h{8 * s:.1f} l{-7 * s:.1f} {20 * s:.1f} h{-8 * s:.1f}z" fill="{RED}"/>' for i in range(3))
    straps = ''.join(f'<path d="{dd}" fill="url(#i-strap)"/><path d="{dd}" fill="url(#i-weave)"/>'
                     for dd in ('M218 -110 L254 168 L272 168 L238 -110Z', 'M322 -110 L286 168 L268 168 L302 -110Z'))
    straps += f'<path d="M226 -110 L259 150" stroke="{RED}" stroke-width="2.4"/><path d="M314 -110 L281 150" stroke="{RED}" stroke-width="2.4"/>'
    clasp = ('<rect x="252" y="160" width="36" height="20" rx="5" fill="url(#i-metal)" stroke="#5d6679"/><rect x="262" y="178" width="16" height="10" fill="url(#i-metal)"/>'
             '<path d="M270 186 C 252 188, 252 214, 270 216 C 288 214, 290 196, 276 194" stroke="url(#i-metal)" stroke-width="6" fill="none" stroke-linecap="round"/>'
             '<circle cx="270" cy="170" r="3" fill="#5d6679"/>')
    card = (f'<rect x="120" y="214" width="300" height="440" rx="24" fill="url(#i-card)" stroke="url(#i-edge2)" stroke-width="1.6"/>'
            f'<rect x="244" y="226" width="52" height="10" rx="5" fill="{NAVY}" stroke="#2b3a60"/>'
            f'<g clip-path="url(#i-cardclip)"><rect x="120" y="214" width="300" height="6" fill="{BLUE}"/><rect x="320" y="214" width="100" height="6" fill="{RED}"/>'
            f'<rect x="120" y="214" width="300" height="440" fill="url(#i-weave)" opacity=".25"/></g>'
            + S.t(144, 266, 'DEV · ID', 'm5', 12, SOFT, ls=.18) + sl(360, 252, .7)
            + f'<rect x="170" y="282" width="200" height="200" rx="20" fill="#0e1a36" stroke="#2b3a60"/><ellipse cx="270" cy="360" rx="110" ry="100" fill="url(#i-gb)"/>'
            + (f'<g clip-path="url(#i-photo)"><image href="{photo}" x="170" y="282" width="200" height="200"/></g>' if photo else '')
            + f'<circle cx="366" cy="290" r="15" fill="url(#i-holo)" opacity=".85"/><circle cx="366" cy="290" r="9" fill="none" stroke="#fff" stroke-opacity=".6"/>'
            + S.t(270, 522, idc['name'], 'd8', fit('d8', idc['name'], 34, 256), WHITE, 'middle', .01)
            + S.t(270, 546, idc['role_line'], 'm5', 11.5, BLUE, 'middle', .12)
            + f'<line x1="144" y1="562" x2="396" y2="562" stroke="{LINE}"/>'
            + S.t(144, 582, 'CLASS', 'm5', 10, MUTED, ls=.14) + S.t(144, 598, idc['class'], 'm5', 13, WHITE)
            + S.t(236, 582, 'BASE', 'm5', 10, MUTED, ls=.14) + S.t(236, 598, idc['base'], 'm5', 13, WHITE)
            + S.t(330, 582, 'UNIV', 'm5', 10, MUTED, ls=.14) + S.t(330, 598, idc['univ'], 'm5', 13, WHITE)
            + f'<g fill="{SOFT}">{barcode(144, 612, 252, 22, user)}</g>' + S.t(270, 646, user, 'm5', 10, MUTED, 'middle', .2)
            + f'<g clip-path="url(#i-cardclip)"><g class="i-sweep"><rect x="60" y="180" width="110" height="520" fill="url(#i-foil)" transform="rotate(18 115 440)"/></g></g>')
    B = S.body
    B.append(f'<g class="i-drop"><g class="i-pend"><g class="i-swing"><g transform="translate(0 64)">{straps}{clasp}{card}</g></g></g></g>')
    X0 = 488
    B.append(f'<g class="i-fade" {d(.2)}>' + S.t(X0, 58, '04', 'm5', 13, BLUE, ls=.14) + S.t(X0 + 30, 58, '/  ID · VERIFIED STATS', 'm5', 13, MUTED, ls=.14) + S.slashes(900, 40) + '</g>')
    B.append(f'<g class="i-rise" {d(.3)}>' + S.t(X0 - 2, 126, 'THE NUMBERS.', 'd8', 62, WHITE) + '</g>')
    B.append(f'<g class="i-rise" {d(.4)}>' + S.t(X0, 156, 'Only facts I can back up · auto-synced from GitHub', 'b4', 16, MUTED) + '</g>')
    tiles = [(idc['cgpa'], idc['cgpa_scale'], 'CGPA', idc['cgpa_sub'], None),
             (str(idc['roles']), '', 'ROLES', idc['roles_sub'], None),
             (str(n), '', 'PROJECTS', f'{live} of them deployed live', n),
             (str(tests), '', 'TESTS', f'automated, across {tested_repos} repo{"" if tested_repos == 1 else "s"}', tests)]
    cw, chh = 218, 124
    for k, (num, suf, lab, sub, cnt) in enumerate(tiles):
        x = X0 + (k % 2) * (cw + 20); y = 180 + (k // 2) * (chh + 18)
        acc = BLUE if k in (0, 3) else RED
        numsvg = count_up(S, x + 22, y + 58, cnt, 'd8', 52, WHITE, begin=.8 + k * .12).replace('c-live', 'i-live').replace('c-still', 'i-still') if cnt else S.t(x + 22, y + 58, num, 'd8', 52, WHITE)
        body = (f'<rect x="{x}" y="{y}" width="{cw}" height="{chh}" rx="18" fill="{PANEL}" stroke="#22304f"/><rect x="{x}" y="{y + 20}" width="3" height="34" rx="1.5" fill="{acc}"/>'
                + numsvg + (S.t(x + 22 + tw('d8', num, 52) + 4, y + 58, suf, 'd6', 24, MUTED) if suf else '')
                + S.t(x + cw - 20, y + 36, lab, 'm5', 11, acc, 'end', .14) + S.t(x + 22, y + 90, sub, 'b4', fit('b4', sub, 15, cw - 40), SOFT))
        if k == 0:
            try: frac = max(0, min(1, float(idc['cgpa']) / float(idc['cgpa_scale'].strip('/'))))
            except ValueError: frac = 0
            bw = cw - 44
            body += (f'<rect x="{x + 22}" y="{y + 104}" width="{bw}" height="5" rx="2.5" fill="#1d2a47"/>'
                     f'<rect x="{x + 22}" y="{y + 104}" width="{bw * frac:.1f}" height="5" rx="2.5" fill="{BLUE}">'
                     f'<animate attributeName="width" values="0;0;{bw * frac:.1f}" keyTimes="0;.4;1" dur="2.4s" begin="0s" fill="freeze" calcMode="spline" keySplines="0 0 1 1;.2 .8 .2 1"/></rect>')
        B.append(f'<g class="i-rise" {d(.55 + k * .12)}>{body}</g>')
    ry0 = 476
    shown = projects[:6]
    label = f'ALL {n} PROJECTS' if n <= 6 else f'6 OF {n} PROJECTS'
    B.append(f'<g class="i-fade" {d(1.1)}>' + S.t(X0, ry0, label, 'm5', 12, SOFT, ls=.16) + S.t(944, ry0, 'github.com/' + user, 'm5', 11, MUTED, 'end') + '</g>')
    book = '<path d="M-6 -7 H5 a1.5 1.5 0 0 1 1.5 1.5 V7 H-4.5 a1.5 1.5 0 0 1 -1.5 -1.5 Z M-6 4 a1.6 1.6 0 0 1 1.5 -1.4 H6.5" stroke="#8a95ad" stroke-width="1.5" fill="none"/>'
    step, rh = 43, 36
    for k, q in enumerate(shown):
        y = ry0 + 16 + k * step
        langs = q.get('idline') or ' · '.join(q['chips'][:2]) or (q.get('language') or '')
        row = (f'<rect x="{X0}" y="{y}" width="456" height="{rh}" rx="11" fill="{PANEL}" stroke="#1f2b47"/>'
               f'<g transform="translate({X0 + 22} {y + rh / 2})">{book}</g>'
               + S.t(X0 + 40, y + 23, q['title'], 'm5', fit('m5', q['title'], 13, 200), WHITE)
               + f'<circle cx="{X0 + 252}" cy="{y + rh / 2}" r="4.5" fill="{lang_dot(q)}"/>'
               + S.t(X0 + 263, y + 23, langs, 'b4', fit('b4', langs, 14, 140), MUTED))
        if q['live']:
            row += pill(S, X0 + 446, y + 9, 'LIVE', True)
        B.append(f'<g class="i-sr" {d(1.2 + k * .08)}>{row}</g>')
    yb = ry0 + 16 + max(1, len(shown)) * step + 6
    if idc.get('highlight'):
        B.append(f'<g class="i-rise" {d(1.8)}><rect x="{X0}" y="{yb}" width="456" height="1" fill="{LINE}"/>'
                 + S.t(X0, yb + 24, idc['highlight'], 'm5', fit('m5', idc['highlight'], 12, 456, .06), '#ffd36b', ls=.06) + '</g>')
    S.h = max(yb + 52, 760)
    S.background(glows=[(270, 420, 300, 'b'), (820, 560, 280, 'r')])
    S.body.insert(0, S.body.pop())
    return S

# ------------------------------------------------------------------ README
START, END = '<!-- PROFILE-SYNC:START -->', '<!-- PROFILE-SYNC:END -->'

def readme_block(cards, stats, versions):
    lines = [START, '', f'![Latest on GitHub, auto-synced: {", ".join(c["title"] for c in cards)}](./latest-repos.svg?v={versions["latest-repos.svg"]})', '',
             '<sub>Updated automatically from my repositories · tap to open</sub>', '', '<p>']
    for i, c in enumerate(cards):
        links = [f'<kbd><a href="{c["url"]}">&lt;/&gt; Code</a></kbd>']
        if c['homepage']: links.insert(0, f'<kbd><a href="{escape(c["homepage"])}">▶ Live Demo</a></kbd>')
        lines.append(f'<b>{escape(c["title"])}</b> &nbsp; ' + ' '.join(links) + ('<br>' if i < len(cards) - 1 else ''))
    top = sorted(stats, key=lambda s: -s['total'])[:8]
    alt = 'Commit activity: ' + ', '.join(f'{s["title"]} {s["total"]} commits' for s in top)
    lines += ['</p>', '', '<br>', '', f'![{alt}](./commit-activity.svg?v={versions["commit-activity.svg"]})', '', END]
    return '\n'.join(lines)

PSTART, PEND = '<!-- PROFILE-SYNC:PROJECTS:START -->', '<!-- PROFILE-SYNC:PROJECTS:END -->'

def projects_block(projects, user, ver):
    lines = [PSTART, f'![Projects: {", ".join(q["title"] for q in projects)}](./projects.svg?v={ver})', '',
             '<sub>Tap to open a project</sub>', '', '<p>']
    for i, q in enumerate(projects):
        links = []
        if q['homepage']: links.append(f'<kbd><a href="{escape(q["homepage"])}">▶ Live Demo</a></kbd>')
        links.append(f'<kbd><a href="{q["url"]}">&lt;/&gt; {"Code" if q["name"] else "Repos"}</a></kbd>')
        lines.append(f'<b>{escape(q["title"])}</b> &nbsp; ' + ' '.join(links) + ('<br>' if i < len(projects) - 1 else ''))
    lines += ['</p>', PEND]
    return '\n'.join(lines)

def splice(text, start, end, block):
    if start in text and end in text:
        return text[:text.index(start)] + block + text[text.index(end) + len(end):]
    print(f'README has no {start} marker; appending that block at the end')
    return text.rstrip() + '\n\n' + block + '\n'

def update_readme(path, cards, stats, versions, projects=None, user=None):
    text = open(path, encoding='utf-8').read()
    text = splice(text, START, END, readme_block(cards, stats, versions))
    if projects is not None and 'projects.svg' in versions:
        text = splice(text, PSTART, PEND, projects_block(projects, user, versions['projects.svg']))
    for name, ver in versions.items():
        text = re.sub(r'(\./' + re.escape(name) + r')\?v=[A-Za-z0-9]+', rf'\1?v={ver}', text)
    open(path, 'w', encoding='utf-8').write(text)

# ------------------------------------------------------------------ main
def title_of(name):
    return ' '.join(w if w.isupper() else w.capitalize() for w in re.split(r'[-_\s]+', name) if w)

def main():
    global A
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=os.path.join(HERE, 'profile.config.json'))
    ap.add_argument('--out', default=HERE, help='folder with README.md where the SVGs are written')
    ap.add_argument('--local-repos', help='test mode: folder of cloned repos + meta.json')
    ap.add_argument('--local-assets', help='test mode: folder containing gfonts/ and si/ checkouts')
    ap.add_argument('--now', help='test mode: pretend today is this ISO date')
    args = ap.parse_args()
    now = parse_dt(args.now) if args.now else datetime.now(timezone.utc)
    cfg = json.load(open(args.config, encoding='utf-8'))
    user = cfg['username']
    A = Assets(args.local_assets)
    src = LocalSource(args.local_repos) if args.local_repos else GitHubSource(user, os.environ.get('GITHUB_TOKEN'))
    exclude_tech = {t.lower() for t in cfg.get('exclude_tech', [])}
    exclude_repos = {r.lower() for r in cfg.get('exclude_repos', [])} | {user.lower()}
    overrides = {k.lower(): v for k, v in cfg.get('repo_overrides', {}).items()}
    keep = lambda t: t.lower() not in exclude_tech

    repos = [r for r in src.repos() if not r.get('fork') and not r.get('archived') and not r.get('private')
             and r['name'].lower() not in exclude_repos]
    repos.sort(key=lambda r: r.get('pushed_at') or '', reverse=True)
    detected_all, cards, report, stats, projects = set(), [], [], [], []
    for i, r in enumerate(repos):
        try:
            techs = {t for t in detect(src, r) if keep(t)}
        except Exception as e:  # one bad repo must not stop the sync
            print(f'  ! {r["name"]}: {e}'); techs = set()
        ov = overrides.get(r['name'].lower(), {})
        techs |= {t for t in ov.get('add', []) if keep(t)}
        techs -= set(ov.get('remove', []))
        detected_all |= techs
        try:
            cs = src.commit_stats(r, now)
        except Exception as e:
            print(f'  ! {r["name"]} commits: {e}'); cs = dict(total=0, weekly=[0] * WEEKS, last=None)
        try:
            tests = count_tests(src, r)
        except Exception as e:
            print(f'  ! {r["name"]} tests: {e}'); tests = 0
        title = ov.get('title') or title_of(r['name'])
        stats.append(dict(name=r['name'], title=title, **cs))
        report.append((r['name'], ordered(techs), cs['total'], sum(cs['weekly']), tests))
        desc = ov.get('description') or r.get('description') or r.get('_readme_blurb') or 'New repository: details coming soon.'
        home = ov.get('homepage', r.get('homepage') or '')
        projects.append(dict(key=r['name'].lower(), name=r['name'], title=title, url=r.get('html_url') or f'https://github.com/{user}/{r["name"]}',
                             homepage=home, live=bool(home), description=desc, short=ov.get('short') or desc,
                             chips=ordered(techs), language=r.get('language') or '', commits=cs['total'], tests=tests))
        if i < cfg.get('latest_count', 4):
            pushed = r.get('pushed_at') or ''
            cards.append(dict(name=r['name'], title=title, commits=cs['total'],
                              description=ov.get('description') or r.get('description') or r.get('_readme_blurb') or 'New repository: details coming soon.',
                              stack=ordered(techs), url=r.get('html_url') or f'https://github.com/{user}/{r["name"]}',
                              homepage=ov.get('homepage', r.get('homepage') or ''), language=r.get('language') or '',
                              stars=r.get('stargazers_count') or 0,
                              updated=datetime.strptime(pushed[:10], '%Y-%m-%d').strftime('updated %b %Y') if pushed else ''))

    # projects that are not public repos (listed in the config) - dropped once a matching repo appears
    for ex in cfg.get('extra_projects', []):
        hints = [h.lower() for h in ex.get('match', [])]
        if any(h in q['name'].lower().replace('_', '-') for q in projects if q['name'] for h in hints): continue
        projects.append(dict(key='extra:' + ex['title'].lower(), name=None, title=ex['title'], url=f'https://github.com/{user}?tab=repositories',
                             homepage=ex.get('homepage', ''), live=bool(ex.get('homepage')), description=ex['description'],
                             short=ex.get('short', ex['description']), chips=[t for t in ex.get('stack', []) if keep(t)],
                             language='', commits=0, tests=0))
    # featured = configured repos first, then the most-committed ones
    featured = []
    byname = {q['key']: q for q in projects}
    for fc in cfg.get('featured', []):
        q = byname.get(fc['repo'].lower())
        if not q or len(featured) == 2: continue
        fill = lambda t: t.replace('{tests}', str(q['tests'])).replace('{commits}', str(q['commits']))
        featured.append(dict(q, title=fc.get('title', q['title']), subtitle=fc.get('subtitle', q['short']),
                             tag=fc.get('tag', 'FEATURED'), points=[fill(x) for x in fc.get('points', [])],
                             chips=[t for t in fc.get('stack', q['chips']) if keep(t)] or q['chips']))
    for q in sorted([q for q in projects if q['name']], key=lambda q: -q['commits']):
        if len(featured) >= 2: break
        if q['key'] in {f['key'] for f in featured}: continue
        featured.append(dict(q, subtitle=wrap('b4', q['short'], 15, 276, 1)[0], tag='FEATURED · ' + (q['chips'][0].upper() if q['chips'] else 'PROJECT'),
                             points=[]))
    fk = [f['key'] for f in featured]
    ordered_projects = [next(f for f in featured if f['key'] == k) for k in fk] + [q for q in projects if q['key'] not in fk]
    SHORT = {'Django REST Framework': 'DRF', 'PostgreSQL': 'Postgres', 'React.js': 'React', 'Express.js': 'Express',
             'GitHub Actions CI': 'CI'}
    for q in ordered_projects:  # short tech line for the ID card list (fits ~140px)
        base = next((p for p in projects if p['key'] == q['key']), q)
        key = [SHORT.get(c, c) for c in base['chips'] if c not in MINOR]
        if len(key) < 2:
            lead = {'Jupyter Notebook': 'Python'}.get(q['language'], q['language']) or next((c for c in q['chips'] if c in ('Python', 'JavaScript', 'Java')), '')
            key = ([lead] if lead and lead not in key else []) + key
        line = ''
        for k2 in key:
            trial = (line + ' · ' + k2) if line else k2
            if line and tw('b4', trial, 14) > 140: break
            line = trial
        q['idline'] = line
    tests_total = sum(q['tests'] for q in projects)
    tested_repos = sum(1 for q in projects if q['tests'])

    # merge new technologies into the curated stack groups
    groups = [dict(g, items=[i for i in g['items'] if keep(i)]) for g in cfg['stack']['groups']]
    present = {i.lower() for g in groups for i in g['items']}
    added = []
    for t in ordered(detected_all):
        aliases = {t.lower()} | {a.lower() for a in T.get(t, ('', []))[1]}
        if aliases & present: continue
        gkey = T.get(t, ('lang', []))[0]
        target = next((g for g in groups if gkey in g.get('keys', [])), groups[-1])
        target['items'].append(t); present.add(t.lower()); added.append(t)

    stack_svg = build_stack(groups, set(cfg['stack'].get('highlight', [])), cfg['stack']['orbits']).render()
    latest_push = max((r.get('pushed_at') or '' for r in repos), default='')
    latest_push = datetime.strptime(latest_push[:10], '%Y-%m-%d').strftime('%d %b %Y') if latest_push else '—'
    latest_svg = build_latest(user, cards, len(repos), latest_push).render()
    commits_svg = build_commits(user, stats).render()
    versions = {}
    outputs = [('stack.svg', stack_svg), ('latest-repos.svg', latest_svg), ('commit-activity.svg', commits_svg)]
    total_commits = sum(st['total'] for st in stats)
    proj_photo = embedded_png(os.path.join(args.out, 'projects.svg'))
    if proj_photo and featured:
        outputs.append(('projects.svg', build_projects(ordered_projects, featured, proj_photo, total_commits).render()))
    else:
        print('  ! projects.svg kept as is (no photo found in the current projects.svg)')
    id_photo = embedded_png(os.path.join(args.out, 'id-dashboard.svg'))
    if id_photo and cfg.get('id_card'):
        outputs.append(('id-dashboard.svg', build_id(user, cfg['id_card'], ordered_projects, tests_total, tested_repos, id_photo).render()))
    else:
        print('  ! id-dashboard.svg kept as is (no photo found in the current id-dashboard.svg)')
    for name, svg in outputs:
        open(os.path.join(args.out, name), 'w', encoding='utf-8').write(svg)
        versions[name] = hashlib.sha1(svg.encode()).hexdigest()[:8]
    update_readme(os.path.join(args.out, 'README.md'), cards, stats, versions, ordered_projects, user)

    lines = ['## Profile sync', '', f'Scanned **{len(repos)}** repositories.', '',
             f'New in stack: {", ".join(added) if added else "nothing new"}', '',
             f'Projects shown: {len(ordered_projects)} ({", ".join(q["title"] for q in ordered_projects)}) · tests found: {tests_total}', '',
             f'| Repo | Commits | Last {WEEKS} weeks | Tests | Detected stack |', '|---|---|---|---|---|']
    lines += [f'| {n} | {c} | {w} | {ts} | {", ".join(t) or "–"} |' for n, t, c, w, ts in report]
    summary = '\n'.join(lines)
    print(summary)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8').write(summary + '\n')

if __name__ == '__main__':
    main()
