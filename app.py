import os, re, csv, io, sqlite3, secrets
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlparse
import requests
from dotenv import load_dotenv
load_dotenv()
from flask import Flask, render_template, request, redirect, url_for, flash, session, Response, jsonify, abort

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv('DATABASE_PATH', os.path.join(APP_DIR, 'tracker.db'))
GRAPH_VERSION = os.getenv('META_GRAPH_VERSION', 'v27.0')
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                  SESSION_COOKIE_SECURE=os.getenv('COOKIE_SECURE','0') == '1')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')
if not ADMIN_PASSWORD and os.getenv('APP_ENV') == 'production':
    raise RuntimeError('ADMIN_PASSWORD must be set in production.')

def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con

def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS campaigns (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
          page_post_id TEXT NOT NULL, source_url TEXT, keyword TEXT NOT NULL,
          created_at TEXT NOT NULL, last_scan_at TEXT
        );
        CREATE TABLE IF NOT EXISTS shares (
          id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id INTEGER NOT NULL,
          facebook_post_id TEXT, author_id TEXT, author_name TEXT, message TEXT,
          permalink TEXT, created_time TEXT, source TEXT NOT NULL DEFAULT 'api',
          status TEXT NOT NULL, checked_at TEXT NOT NULL,
          UNIQUE(campaign_id, facebook_post_id),
          FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_shares_campaign ON shares(campaign_id);
        CREATE INDEX IF NOT EXISTS idx_shares_author ON shares(campaign_id, author_id);
        """)

def csrf_token():
    token = session.get('_csrf')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf'] = token
    return token
app.jinja_env.globals['csrf_token'] = csrf_token

@app.before_request
def protect_forms():
    if request.method == 'POST':
        supplied = request.form.get('_csrf', '')
        expected = session.get('_csrf', '')
        if not expected or not secrets.compare_digest(supplied, expected):
            abort(400, 'CSRF token không hợp lệ. Hãy tải lại trang và thử lại.')

def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return fn(*args, **kwargs)
    return wrapped

def keyword_valid(message, keyword):
    return bool(message and keyword and keyword.casefold() in message.casefold())

def graph_get(url, params=None):
    token = os.getenv('META_PAGE_ACCESS_TOKEN', '').strip()
    if not token:
        raise RuntimeError('Chưa cấu hình META_PAGE_ACCESS_TOKEN ở hosting.')
    if urlparse(url).hostname not in ('graph.facebook.com', None):
        raise RuntimeError('Meta trả về URL phân trang không hợp lệ; đã dừng để bảo vệ access token.')
    response = requests.get(url, params=params, timeout=45)
    try:
        payload = response.json()
    except Exception:
        raise RuntimeError(f'Meta trả về HTTP {response.status_code} không phải JSON.')
    if response.status_code >= 400 or 'error' in payload:
        error = payload.get('error', {})
        raise RuntimeError(f"Meta API error {error.get('code','unknown')}: {error.get('message', f'HTTP {response.status_code}')}")
    return payload

def run_scan(campaign_id):
    with db() as con:
        campaign = con.execute('SELECT * FROM campaigns WHERE id=?', (campaign_id,)).fetchone()
    if not campaign:
        raise RuntimeError('Không tìm thấy chiến dịch.')
    post_id = campaign['page_post_id'].strip()
    if not re.fullmatch(r'[0-9_]+', post_id):
        raise RuntimeError('Post ID không hợp lệ. Cần Graph API object ID, thường dạng PAGE_ID_POST_ID.')
    url = f'https://graph.facebook.com/{GRAPH_VERSION}/{post_id}/sharedposts'
    params = {'fields':'id,message,permalink_url,created_time,from','limit':'100',
              'access_token':os.getenv('META_PAGE_ACCESS_TOKEN','').strip()}
    pages = scanned = inserted = updated = 0
    seen = set()
    with db() as con:
        while url and pages < 1000:
            payload = graph_get(url, params if pages == 0 else None)
            pages += 1
            for item in payload.get('data', []):
                fid = str(item.get('id') or '')
                if not fid or fid in seen:
                    continue
                seen.add(fid); scanned += 1
                author = item.get('from') or {}
                message = item.get('message') or ''
                status = 'valid' if keyword_valid(message, campaign['keyword']) else ('invalid' if message else 'needs_review')
                checked = datetime.now(timezone.utc).isoformat()
                values = (campaign_id,fid,author.get('id'),author.get('name'),message,
                          item.get('permalink_url'),item.get('created_time'),'api',status,checked)
                cur = con.execute("""INSERT OR IGNORE INTO shares
                  (campaign_id,facebook_post_id,author_id,author_name,message,permalink,created_time,source,status,checked_at)
                  VALUES (?,?,?,?,?,?,?,?,?,?)""", values)
                if cur.rowcount:
                    inserted += 1
                else:
                    con.execute("""UPDATE shares SET author_id=?,author_name=?,message=?,permalink=?,
                      created_time=?,status=?,checked_at=? WHERE campaign_id=? AND facebook_post_id=?""",
                      (author.get('id'),author.get('name'),message,item.get('permalink_url'),
                       item.get('created_time'),status,checked,campaign_id,fid))
                    updated += 1
            url = (payload.get('paging') or {}).get('next')
        con.execute('UPDATE campaigns SET last_scan_at=? WHERE id=?',
                    (datetime.now(timezone.utc).isoformat(),campaign_id))
    if pages >= 1000 and url:
        raise RuntimeError('Đã chạm giới hạn an toàn 1.000 trang; hãy quét lại sau.')
    return {'pages':pages,'scanned':scanned,'inserted':inserted,'updated':updated}

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        if ADMIN_PASSWORD and secrets.compare_digest(request.form.get('password',''), ADMIN_PASSWORD):
            session.clear()
            session['logged_in'] = True
            return redirect(url_for('index'))
        flash('Mật khẩu không đúng hoặc chưa được cấu hình.', 'error')
    return render_template('login.html')

@app.post('/logout')
@login_required
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.get('/')
@login_required
def index():
    with db() as con:
        campaigns = con.execute("""SELECT c.*,COUNT(s.id) total,
          SUM(CASE WHEN s.status='valid' THEN 1 ELSE 0 END) valid,
          SUM(CASE WHEN s.status='invalid' THEN 1 ELSE 0 END) invalid,
          SUM(CASE WHEN s.status='needs_review' THEN 1 ELSE 0 END) review
          FROM campaigns c LEFT JOIN shares s ON s.campaign_id=c.id
          GROUP BY c.id ORDER BY c.id DESC""").fetchall()
    return render_template('index.html',campaigns=campaigns,api_ready=bool(os.getenv('META_PAGE_ACCESS_TOKEN')))

@app.post('/campaigns')
@login_required
def create_campaign():
    name=request.form.get('name','').strip()
    post_id=request.form.get('page_post_id','').strip()
    keyword=request.form.get('keyword','').strip()
    source_url=request.form.get('source_url','').strip()
    if not name or not post_id or not keyword:
        flash('Cần nhập tên chiến dịch, Post ID và từ khóa.','error')
        return redirect(url_for('index'))
    if not re.fullmatch(r'[0-9_]+',post_id):
        flash('Post ID cần là Graph API object ID, không phải URL Facebook.','error')
        return redirect(url_for('index'))
    with db() as con:
        con.execute('INSERT INTO campaigns(name,page_post_id,source_url,keyword,created_at) VALUES(?,?,?,?,?)',
                    (name,post_id,source_url,keyword,datetime.now(timezone.utc).isoformat()))
    flash('Đã tạo chiến dịch.','success')
    return redirect(url_for('index'))

@app.post('/campaigns/<int:campaign_id>/scan')
@login_required
def scan(campaign_id):
    try:
        result=run_scan(campaign_id)
        flash(f"Quét xong: {result['scanned']} lượt đọc, {result['inserted']} mới, {result['updated']} cập nhật, {result['pages']} trang.",'success')
    except Exception as error:
        flash(str(error),'error')
    return redirect(url_for('campaign_detail',campaign_id=campaign_id))

@app.get('/campaigns/<int:campaign_id>')
@login_required
def campaign_detail(campaign_id):
    q=request.args.get('q','').strip()
    status=request.args.get('status','all')
    with db() as con:
        campaign=con.execute('SELECT * FROM campaigns WHERE id=?',(campaign_id,)).fetchone()
        if not campaign: return 'Not found',404
        sql='SELECT * FROM shares WHERE campaign_id=?'; args=[campaign_id]
        if q:
            sql+=' AND (author_name LIKE ? OR message LIKE ? OR author_id LIKE ?)'; args += [f'%{q}%']*3
        if status in ('valid','invalid','needs_review'):
            sql+=' AND status=?'; args.append(status)
        sql+=' ORDER BY COALESCE(created_time,checked_at) DESC LIMIT 2000'
        shares=con.execute(sql,args).fetchall()
        stats=con.execute("""SELECT COUNT(*) total,
          SUM(CASE WHEN status='valid' THEN 1 ELSE 0 END) valid,
          SUM(CASE WHEN status='invalid' THEN 1 ELSE 0 END) invalid,
          SUM(CASE WHEN status='needs_review' THEN 1 ELSE 0 END) review,
          COUNT(DISTINCT author_id) authors FROM shares WHERE campaign_id=?""",(campaign_id,)).fetchone()
        leaderboard=con.execute("""SELECT COALESCE(author_name,'Chưa xác định') author_name,author_id,
          COUNT(*) total,SUM(CASE WHEN status='valid' THEN 1 ELSE 0 END) valid
          FROM shares WHERE campaign_id=? GROUP BY author_id,author_name ORDER BY valid DESC,total DESC LIMIT 100""",(campaign_id,)).fetchall()
    return render_template('campaign.html',campaign=campaign,shares=shares,stats=stats,leaderboard=leaderboard,q=q,status=status)

@app.post('/campaigns/<int:campaign_id>/manual')
@login_required
def manual_add(campaign_id):
    permalink=request.form.get('permalink','').strip()
    author=request.form.get('author_name','').strip()
    message=request.form.get('message','').strip()
    parsed=urlparse(permalink)
    if parsed.scheme!='https' or parsed.hostname not in ('www.facebook.com','m.facebook.com','facebook.com'):
        flash('Chỉ chấp nhận link Facebook HTTPS.','error')
        return redirect(url_for('campaign_detail',campaign_id=campaign_id))
    with db() as con:
        campaign=con.execute('SELECT * FROM campaigns WHERE id=?',(campaign_id,)).fetchone()
        if not campaign: return 'Not found',404
        status='valid' if keyword_valid(message,campaign['keyword']) else ('invalid' if message else 'needs_review')
        con.execute("""INSERT INTO shares(campaign_id,author_name,message,permalink,source,status,checked_at)
          VALUES(?,?,?,?,'manual',?,?)""",(campaign_id,author or 'Chưa xác định',message,permalink,status,datetime.now(timezone.utc).isoformat()))
    flash('Đã thêm dữ liệu thủ công; chưa được Meta API xác minh.','success')
    return redirect(url_for('campaign_detail',campaign_id=campaign_id))

@app.get('/campaigns/<int:campaign_id>/export.csv')
@login_required
def export_csv(campaign_id):
    with db() as con:
        campaign=con.execute('SELECT * FROM campaigns WHERE id=?',(campaign_id,)).fetchone()
        if not campaign: return 'Not found',404
        rows=con.execute('SELECT author_name,author_id,message,permalink,created_time,source,status FROM shares WHERE campaign_id=? ORDER BY created_time DESC',(campaign_id,)).fetchall()
    output=io.StringIO(); writer=csv.writer(output)
    writer.writerow(['Tên tài khoản','Facebook ID','Nội dung share','Link share','Thời gian','Nguồn','Trạng thái'])
    for row in rows: writer.writerow(list(row))
    return Response('\ufeff'+output.getvalue(),mimetype='text/csv; charset=utf-8',
      headers={'Content-Disposition':f'attachment; filename="campaign-{campaign_id}-shares.csv"'})

@app.get('/health')
def health():
    return jsonify(status='ok',database=os.path.exists(DB_PATH),meta_token_configured=bool(os.getenv('META_PAGE_ACCESS_TOKEN')))

init_db()
if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.getenv('PORT','5000')),debug=False)
