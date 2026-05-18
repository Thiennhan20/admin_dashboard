# Deploy Django Admin to Railway

## 1. Local build check

Run from `admin_en_website/admin_pannel`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe manage.py collectstatic --noinput --clear
```

Optional production check:

```powershell
$env:DEBUG="False"
$env:SECRET_KEY="replace-with-a-long-production-secret"
.\.venv\Scripts\python.exe manage.py check --deploy
Remove-Item Env:DEBUG
Remove-Item Env:SECRET_KEY
```

## 2. Railway service settings

If deploying from this monorepo through GitHub, set the Railway service root directory to:

```text
admin_en_website/admin_pannel
```

Railway will use the `Procfile` in this folder:

```text
web: python manage.py migrate --noinput && python manage.py collectstatic --noinput && gunicorn admin_pannel.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --timeout 120
```

## 3. Railway variables

Add these to the Django admin service variables:

```env
DEBUG=False
SECRET_KEY=replace-with-a-long-random-production-secret
DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/DATABASE

CLIENT_URL=https://moviesaw.vercel.app
SERVER_URL=https://server-nextjs-firm.onrender.com
API_BASE_URL=https://server-nextjs-firm.onrender.com/api
API_AUTH_BASE_URL=https://server-nextjs-firm.onrender.com/api/auth

MONGODB_URI=your-mongodb-uri
MONGODB_NAME=your-mongodb-database-name

ALLOWED_HOSTS=.railway.app,.up.railway.app
CSRF_TRUSTED_ORIGINS=https://*.railway.app,https://*.up.railway.app

DB_WAIT_TIMEOUT=60
DB_WAIT_INTERVAL=3
```

After Railway generates your public domain, add it to:

```env
ALLOWED_HOSTS=.railway.app,.up.railway.app,your-admin-domain.up.railway.app
CSRF_TRUSTED_ORIGINS=https://*.railway.app,https://*.up.railway.app,https://your-admin-domain.up.railway.app
```

## 4. Deploy commands with Railway CLI

From `admin_en_website/admin_pannel`:

```powershell
railway login
railway link
railway up --path . --path-as-root
```

Or deploy from GitHub:

1. Push code to GitHub.
2. Railway -> New Project -> Deploy from GitHub repo.
3. Set Root Directory to `admin_en_website/admin_pannel`.
4. Add variables above.
5. Deploy.
6. Settings -> Networking -> Generate Domain.

## 5. After deploy

Check:

```text
https://your-admin-domain.up.railway.app/health/
https://your-admin-domain.up.railway.app/dashboard/login/
```
