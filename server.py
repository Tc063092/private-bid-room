import os, sqlite3, secrets, hashlib, hmac, json, time, datetime, html, threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from http.cookies import SimpleCookie
from decimal import Decimal, InvalidOperation

DB=os.environ.get('AUCTION_DB','auction.sqlite3')
ADMIN_PASSWORD=os.environ.get('AUCTION_ADMIN_PASSWORD','')
HOST=os.environ.get('HOST','0.0.0.0'); PORT=int(os.environ.get('PORT','8000'))
LOCK=threading.RLock()

def conn():
 c=sqlite3.connect(DB,timeout=20); c.row_factory=sqlite3.Row; return c

def pw_hash(p,salt=None):
 salt=salt or secrets.token_hex(16)
 return salt+':'+hashlib.scrypt(p.encode(),salt=bytes.fromhex(salt),n=2**14,r=8,p=1).hex()

def verify(p,stored):
 try: return hmac.compare_digest(pw_hash(p,stored.split(':')[0]),stored)
 except Exception: return False

def init():
 with conn() as c:
  c.executescript('''CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), title TEXT NOT NULL, start_cents INTEGER NOT NULL, increment_cents INTEGER NOT NULL, closes_at INTEGER, closed INTEGER NOT NULL DEFAULT 0); CREATE TABLE IF NOT EXISTS members(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, pin_hash TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0); CREATE TABLE IF NOT EXISTS bids(id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL UNIQUE, amount_cents INTEGER NOT NULL, visibility TEXT NOT NULL CHECK(visibility IN ('public','hidden')), updated_at INTEGER NOT NULL, FOREIGN KEY(member_id) REFERENCES members(id)); CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, member_id INTEGER, is_admin INTEGER NOT NULL, expires INTEGER NOT NULL);''')
  columns=[r['name'] for r in c.execute('PRAGMA table_info(members)').fetchall()]
  if 'approved' not in columns: c.execute('ALTER TABLE members ADD COLUMN approved INTEGER NOT NULL DEFAULT 0')
  setting_columns=[r['name'] for r in c.execute('PRAGMA table_info(settings)').fetchall()]
  if 'started' not in setting_columns: c.execute('ALTER TABLE settings ADD COLUMN started INTEGER NOT NULL DEFAULT 1')
  c.execute("INSERT OR IGNORE INTO settings (id,title,start_cents,increment_cents,closes_at,closed,started) VALUES(1,'Friends Private Auction',100000,10000,NULL,0,0)")
  c.executescript('''CREATE TABLE IF NOT EXISTS auction_archive(id INTEGER PRIMARY KEY AUTOINCREMENT, archived_at INTEGER NOT NULL, title TEXT NOT NULL, start_cents INTEGER NOT NULL, increment_cents INTEGER NOT NULL, closes_at INTEGER, closed INTEGER NOT NULL, bids_json TEXT NOT NULL, winner_json TEXT); CREATE TABLE IF NOT EXISTS bid_removals(id INTEGER PRIMARY KEY AUTOINCREMENT, removed_at INTEGER NOT NULL, member_id INTEGER NOT NULL, member_name TEXT NOT NULL, amount_cents INTEGER NOT NULL, visibility TEXT NOT NULL, reason TEXT NOT NULL);''')

def money(v):
 try:
  d=Decimal(str(v))
  if not d.is_finite() or d<=0 or d.as_tuple().exponent < -2: raise ValueError()
  cents=int(d*100)
  if cents>100000000000: raise ValueError()
  return cents
 except (InvalidOperation,ValueError,TypeError): raise ValueError('Enter a valid positive amount with at most 2 decimal places')

def state(c,member_id=None,admin=False):
 s=c.execute('SELECT * FROM settings WHERE id=1').fetchone(); now=int(time.time()); closed=bool(s['closed'] or (s['closes_at'] and now>=s['closes_at']))
 rows=c.execute('SELECT m.id,m.name,b.amount_cents,b.visibility,b.updated_at,b.id AS bid_id FROM bids b JOIN members m ON m.id=b.member_id ORDER BY b.updated_at ASC,b.id ASC').fetchall()
 winner=sorted(rows,key=lambda r:(-r['amount_cents'],r['updated_at'],r['bid_id']))[0] if rows else None
 visible=[]
 for r in rows:
  own=member_id==r['id']; see=admin or own or r['visibility']=='public'
  visible.append({'name':r['name'],'amount':r['amount_cents']/100 if see else None,'visibility':r['visibility'],'mine':own,'member_id':r['id'] if admin else None,'updated_at':r['updated_at'] if see else None})
 return {'title':s['title'],'start':s['start_cents']/100,'increment':s['increment_cents']/100,'closes_at':s['closes_at'],'closed':closed,'started':bool(s['started']),'count':len(rows),'bids':visible,'winner':{'name':winner['name'],'amount':winner['amount_cents']/100} if closed and winner else None,'admin':admin,'logged_in':bool(member_id),'my_name':c.execute('SELECT name FROM members WHERE id=?',(member_id,)).fetchone()['name'] if member_id else None,'approved':bool(c.execute('SELECT approved FROM members WHERE id=?',(member_id,)).fetchone()['approved']) if member_id else False,'archives':[{'id':r['id'],'title':r['title'],'archived_at':r['archived_at'],'winner':json.loads(r['winner_json']) if r['winner_json'] else None,'bid_count':len(json.loads(r['bids_json']))} for r in c.execute('SELECT * FROM auction_archive ORDER BY id DESC LIMIT 20').fetchall()] if admin else [],'pending_members':[dict(r) for r in c.execute('SELECT id,name FROM members WHERE approved=0 ORDER BY id').fetchall()] if admin else []}

class Handler(BaseHTTPRequestHandler):
 def send(self,status,data,cookie=None):
  body=json.dumps(data).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Content-Length',str(len(body)))
  if cookie: self.send_header('Set-Cookie',cookie)
  self.end_headers(); self.wfile.write(body)
 def auth(self,c):
  cookie=SimpleCookie()
  try: cookie.load(self.headers.get('Cookie',''))
  except Exception: pass
  token=cookie.get('auction_session'); token=token.value if token else ''
  if not token: return None,False
  row=c.execute('SELECT member_id,is_admin FROM sessions WHERE token_hash=? AND expires>?',(hashlib.sha256(token.encode()).hexdigest(),int(time.time()))).fetchone()
  return (row['member_id'],bool(row['is_admin'])) if row else (None,False)
 def session(self,c,member_id=None,admin=False):
  token=secrets.token_urlsafe(32)
  c.execute('INSERT INTO sessions VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),member_id,int(admin),int(time.time())+86400*14))
  secure='; Secure' if self.headers.get('X-Forwarded-Proto')=='https' else ''
  return 'auction_session='+token+'; HttpOnly; SameSite=Lax; Path=/; Max-Age=1209600'+secure
 def do_GET(self):
  path=urlparse(self.path).path
  if path=='/':
   data=open(os.path.join(os.path.dirname(__file__),'index.html'),'rb').read(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); return
  if path=='/api/state':
   with conn() as c:
    m,a=self.auth(c); self.send(200,state(c,m,a)); return
  self.send(404,{'error':'Not found'})
 def do_POST(self):
  path=urlparse(self.path).path
  if self.headers.get('Content-Type','').split(';')[0]!='application/json': return self.send(415,{'error':'JSON required'})
  try:
   length=int(self.headers.get('Content-Length','0'))
   if length>10000 or length<=0: raise ValueError('Invalid request size')
   data=json.loads(self.rfile.read(length))
   with LOCK,conn() as c:
    m,a=self.auth(c)
    if path=='/api/register':
     name=str(data.get('name','')).strip(); pin=str(data.get('pin',''))
     if not 2<=len(name)<=50 or not 4<=len(pin)<=64: raise ValueError('Name must be 2–50 characters and PIN 4–64 characters')
     if c.execute('SELECT 1 FROM members WHERE name=?',(name,)).fetchone(): raise ValueError('Name already taken; log in instead')
     cur=c.execute('INSERT INTO members(name,pin_hash) VALUES(?,?)',(name,pw_hash(pin)))
     self.send(200,{'ok':True,'message':'Registration received. Wait for organizer approval before bidding.'},self.session(c,cur.lastrowid)); return
    if path=='/api/login':
     row=c.execute('SELECT * FROM members WHERE name=?',(str(data.get('name','')).strip(),)).fetchone()
     if not row or not verify(str(data.get('pin','')),row['pin_hash']): return self.send(401,{'error':'Invalid name or PIN'})
     self.send(200,{'ok':True},self.session(c,row['id'])); return
    if path=='/api/admin-login':
     if not ADMIN_PASSWORD: return self.send(503,{'error':'Set AUCTION_ADMIN_PASSWORD on the server first'})
     if not hmac.compare_digest(str(data.get('password','')),ADMIN_PASSWORD): return self.send(401,{'error':'Wrong organizer password'})
     self.send(200,{'ok':True},self.session(c,admin=True)); return
    if path=='/api/logout':
     cookie=SimpleCookie(); cookie.load(self.headers.get('Cookie',''))
     if cookie.get('auction_session'): c.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(cookie['auction_session'].value.encode()).hexdigest(),))
     return self.send(200,{'ok':True},'auction_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0')
    if path=='/api/bid':
     if not m or a: return self.send(403,{'error':'Member login required'})
     if not c.execute('SELECT approved FROM members WHERE id=?',(m,)).fetchone()['approved']: return self.send(403,{'error':'Waiting for organizer approval'})
     s=c.execute('SELECT * FROM settings WHERE id=1').fetchone()
     if not s['started']: raise ValueError('Auction has not started yet')
     if s['closed'] or (s['closes_at'] and time.time()>=s['closes_at']): raise ValueError('Auction is closed')
     amt=money(data.get('amount')); vis=data.get('visibility')
     if vis not in ('public','hidden'): raise ValueError('Choose Public or Hidden')
     prior=c.execute('SELECT amount_cents FROM bids WHERE member_id=?',(m,)).fetchone()
     minimum=prior['amount_cents']+s['increment_cents'] if prior else s['start_cents']
     if amt<minimum: raise ValueError(f'Minimum allowed bid is RM {minimum/100:,.2f}')
     c.execute('INSERT INTO bids(member_id,amount_cents,visibility,updated_at) VALUES(?,?,?,?) ON CONFLICT(member_id) DO UPDATE SET amount_cents=excluded.amount_cents,visibility=excluded.visibility,updated_at=excluded.updated_at',(m,amt,vis,int(time.time())))
     return self.send(200,{'ok':True})
    if path=='/api/approval':
     if not a: return self.send(403,{'error':'Organizer login required'})
     member_id=int(data.get('member_id',0)); action=data.get('action')
     if action=='approve':
      cur=c.execute('UPDATE members SET approved=1 WHERE id=? AND approved=0',(member_id,))
     elif action=='reject':
      if c.execute('SELECT 1 FROM bids WHERE member_id=?',(member_id,)).fetchone(): raise ValueError('Cannot reject a member with a bid')
      c.execute('DELETE FROM sessions WHERE member_id=?',(member_id,))
      cur=c.execute('DELETE FROM members WHERE id=? AND approved=0',(member_id,))
     else: raise ValueError('Invalid approval action')
     if cur.rowcount!=1: raise ValueError('Pending member not found')
     return self.send(200,{'ok':True})
    if path=='/api/settings':
     if not a: return self.send(403,{'error':'Organizer login required'})
     s=c.execute('SELECT closed FROM settings WHERE id=1').fetchone()
     if s['closed']: raise ValueError('Auction already closed')
     title=str(data.get('title','')).strip()
     if not 3<=len(title)<=100: raise ValueError('Title must be 3–100 characters')
     start=money(data.get('start')); increment=money(data.get('increment'))
     close=data.get('closes_at'); close=int(close) if close not in ('',None) else None
     if close is not None and close<=time.time(): raise ValueError('Deadline must be in the future')
     maxbid=c.execute('SELECT MAX(amount_cents) AS x FROM bids').fetchone()['x']
     if maxbid and start>maxbid: raise ValueError('Starting price cannot exceed existing bids')
     c.execute('UPDATE settings SET title=?,start_cents=?,increment_cents=?,closes_at=? WHERE id=1',(title,start,increment,close))
     return self.send(200,{'ok':True})
    if path=='/api/remove-bid':
     if not a: return self.send(403,{'error':'Organizer login required'})
     member_id=int(data.get('member_id',0)); reason=str(data.get('reason','')).strip()
     if not 3<=len(reason)<=250: raise ValueError('Provide a reason (3–250 characters)')
     row=c.execute('SELECT b.*,m.name FROM bids b JOIN members m ON m.id=b.member_id WHERE b.member_id=?',(member_id,)).fetchone()
     if not row: raise ValueError('Bid not found')
     c.execute('INSERT INTO bid_removals(removed_at,member_id,member_name,amount_cents,visibility,reason) VALUES(?,?,?,?,?,?)',(int(time.time()),member_id,row['name'],row['amount_cents'],row['visibility'],reason))
     c.execute('DELETE FROM bids WHERE member_id=?',(member_id,))
     return self.send(200,{'ok':True})
    if path=='/api/new-auction':
     if not a: return self.send(403,{'error':'Organizer login required'})
     title=str(data.get('title','')).strip()
     if not 3<=len(title)<=100: raise ValueError('Title must be 3–100 characters')
     start=money(data.get('start')); increment=money(data.get('increment'))
     close=data.get('closes_at'); close=int(close) if close not in ('',None) else None
     if close is not None and close<=time.time(): raise ValueError('New deadline must be in the future')
     old=c.execute('SELECT * FROM settings WHERE id=1').fetchone()
     if not (old['closed'] or (old['closes_at'] and time.time()>=old['closes_at'])): raise ValueError('Close the current auction before creating a new one')
     rows=c.execute('SELECT m.name,b.amount_cents,b.visibility,b.updated_at,b.id FROM bids b JOIN members m ON m.id=b.member_id ORDER BY b.id').fetchall()
     history=[dict(r) for r in rows]
     winner=sorted(history,key=lambda r:(-r['amount_cents'],r['updated_at'],r['id']))[0] if history else None
     c.execute('INSERT INTO auction_archive(archived_at,title,start_cents,increment_cents,closes_at,closed,bids_json,winner_json) VALUES(?,?,?,?,?,?,?,?)',(int(time.time()),old['title'],old['start_cents'],old['increment_cents'],old['closes_at'],int(bool(old['closed'] or (old['closes_at'] and time.time()>=old['closes_at']))),json.dumps(history),json.dumps({'name':winner['name'],'amount':winner['amount_cents']/100}) if winner else None))
     c.execute('DELETE FROM bids')
     c.execute('UPDATE settings SET title=?,start_cents=?,increment_cents=?,closes_at=?,closed=0,started=0 WHERE id=1',(title,start,increment,close))
     return self.send(200,{'ok':True})
    if path=='/api/start':
     if not a: return self.send(403,{'error':'Organizer login required'})
     s=c.execute('SELECT * FROM settings WHERE id=1').fetchone()
     if s['closed'] or (s['closes_at'] and time.time()>=s['closes_at']): raise ValueError('Auction is closed; create a new auction')
     if s['started']: raise ValueError('Auction already started')
     c.execute('UPDATE settings SET started=1 WHERE id=1')
     return self.send(200,{'ok':True})
    if path=='/api/close':
     if not a: return self.send(403,{'error':'Organizer login required'})
     c.execute('UPDATE settings SET closed=1 WHERE id=1'); return self.send(200,{'ok':True})
    return self.send(404,{'error':'Unknown action'})
  except (ValueError,TypeError,KeyError,OverflowError) as e: return self.send(400,{'error':str(e)})
  except sqlite3.IntegrityError: return self.send(400,{'error':'Name already taken'})

if __name__=='__main__':
 init()
 if not ADMIN_PASSWORD: print('WARNING: organizer login disabled until AUCTION_ADMIN_PASSWORD is set')
 print(f'Open http://localhost:{PORT}')
 ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
