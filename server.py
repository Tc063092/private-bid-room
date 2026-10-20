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
  if 'member_limit' not in setting_columns: c.execute('ALTER TABLE settings ADD COLUMN member_limit INTEGER')
  setting_columns=[r['name'] for r in c.execute('PRAGMA table_info(settings)').fetchall()]
  if 'started' not in setting_columns: c.execute('ALTER TABLE settings ADD COLUMN started INTEGER NOT NULL DEFAULT 1')
  if 'winner_json' not in setting_columns: c.execute('ALTER TABLE settings ADD COLUMN winner_json TEXT')
  c.execute("INSERT OR IGNORE INTO settings (id,title,start_cents,increment_cents,closes_at,closed,started) VALUES(1,'Friends Private Auction',100000,10000,NULL,0,0)")
  c.execute('''CREATE TABLE IF NOT EXISTS member_admin_audit(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL, member_id INTEGER NOT NULL, old_name TEXT NOT NULL, new_name TEXT, action TEXT NOT NULL, had_bid INTEGER NOT NULL)''')
  c.execute('''CREATE TABLE IF NOT EXISTS archive_deletion_audit(id INTEGER PRIMARY KEY AUTOINCREMENT, archive_id INTEGER NOT NULL, auction_title TEXT NOT NULL, deleted_at INTEGER NOT NULL)''')
  c.execute('''CREATE TABLE IF NOT EXISTS roulette_entries(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE, weight INTEGER NOT NULL CHECK(weight BETWEEN 1 AND 1000000))''')
  # Migrate legacy CHECK(weight BETWEEN 1...) to allow zero-probability participants.
  roulette_schema=c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='roulette_entries'").fetchone()[0]
  if 'BETWEEN 1 AND' in roulette_schema.upper():
   c.execute('ALTER TABLE roulette_entries RENAME TO roulette_entries_legacy')
   c.execute('CREATE TABLE roulette_entries(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE, weight INTEGER NOT NULL CHECK(weight BETWEEN 0 AND 1000000))')
   c.execute('INSERT INTO roulette_entries(id,name,weight) SELECT id,name,weight FROM roulette_entries_legacy')
   c.execute('DROP TABLE roulette_entries_legacy')
  c.execute('''CREATE TABLE IF NOT EXISTS roulette_saved_rounds(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,entries_json TEXT NOT NULL,saved_at INTEGER NOT NULL)''')
  if 'roulette_title' not in [r['name'] for r in c.execute('PRAGMA table_info(settings)').fetchall()]: c.execute("ALTER TABLE settings ADD COLUMN roulette_title TEXT NOT NULL DEFAULT 'Casino Roulette'")
  c.execute('''CREATE TABLE IF NOT EXISTS roulette_history_deleted(drawn_at INTEGER NOT NULL,winner_name TEXT NOT NULL,PRIMARY KEY(drawn_at,winner_name))''')
  c.execute('''CREATE TABLE IF NOT EXISTS roulette_history(id INTEGER PRIMARY KEY AUTOINCREMENT, auction_title TEXT NOT NULL, winner_name TEXT NOT NULL, drawn_at INTEGER NOT NULL)''')
  for col in ('roulette_starts_at','roulette_ends_at'):
   if col not in [r['name'] for r in c.execute('PRAGMA table_info(settings)').fetchall()]: c.execute(f'ALTER TABLE settings ADD COLUMN {col} INTEGER')
  if 'names_json' not in [r['name'] for r in c.execute('PRAGMA table_info(roulette_history)').fetchall()]: c.execute('ALTER TABLE roulette_history ADD COLUMN names_json TEXT')
  if 'roulette_result_json' not in [r['name'] for r in c.execute('PRAGMA table_info(settings)').fetchall()]: c.execute('ALTER TABLE settings ADD COLUMN roulette_result_json TEXT')
  c.executescript('''CREATE TABLE IF NOT EXISTS auction_archive(id INTEGER PRIMARY KEY AUTOINCREMENT, archived_at INTEGER NOT NULL, title TEXT NOT NULL, start_cents INTEGER NOT NULL, increment_cents INTEGER NOT NULL, closes_at INTEGER, closed INTEGER NOT NULL, bids_json TEXT NOT NULL, winner_json TEXT); CREATE TABLE IF NOT EXISTS bid_removals(id INTEGER PRIMARY KEY AUTOINCREMENT, removed_at INTEGER NOT NULL, member_id INTEGER NOT NULL, member_name TEXT NOT NULL, amount_cents INTEGER NOT NULL, visibility TEXT NOT NULL, reason TEXT NOT NULL);''')

def money(v):
 try:
  d=Decimal(str(v))
  if not d.is_finite() or d<=0 or d.as_tuple().exponent < -2: raise ValueError()
  cents=int(d*100)
  if cents>100000000000: raise ValueError()
  return cents
 except (InvalidOperation,ValueError,TypeError): raise ValueError('Enter a valid positive amount with at most 2 decimal places')

def finalize_winner(c):
 """Persist one cryptographically random draw for the tied highest bids."""
 s=c.execute('SELECT * FROM settings WHERE id=1').fetchone()
 if not (s['closed'] or (s['closes_at'] and time.time()>=s['closes_at'])):
  return None
 if s['winner_json'] is not None:
  return json.loads(s['winner_json'])
 rows=c.execute('SELECT m.name,b.amount_cents FROM bids b JOIN members m ON m.id=b.member_id').fetchall()
 winner=None
 if rows:
  maximum=max(r['amount_cents'] for r in rows)
  tied=[r for r in rows if r['amount_cents']==maximum]
  selected=secrets.choice(tied)
  winner={'name':selected['name'],'amount':maximum/100,'tie_count':len(tied),'tie_break':'random_draw' if len(tied)>1 else 'highest_bid'}
 c.execute('UPDATE settings SET winner_json=? WHERE id=1 AND winner_json IS NULL',(json.dumps(winner),))
 return winner

def reconcile_roulette_history(c):
 s=c.execute('SELECT roulette_result_json,roulette_title FROM settings WHERE id=1').fetchone()
 if not s or not s['roulette_result_json']: return
 try:
  result=json.loads(s['roulette_result_json'])
  winner=result['name']; drawn=int(result.get('drawn_at') or time.time())
 except (ValueError,KeyError,TypeError): return
 existing=c.execute('SELECT id FROM roulette_history WHERE winner_name=? AND drawn_at=?',(winner,drawn)).fetchone()
 if not existing and not c.execute('SELECT 1 FROM roulette_history_deleted WHERE drawn_at=? AND winner_name=?',(drawn,winner)).fetchone():
  names=[r['name'] for r in c.execute('SELECT name FROM roulette_entries ORDER BY id')]
  c.execute('INSERT INTO roulette_history(auction_title,winner_name,drawn_at,names_json) VALUES(?,?,?,?)',(s['roulette_title'] or 'Casino Roulette',winner,drawn,json.dumps(names)))

def state(c,member_id=None,admin=False):
 reconcile_roulette_history(c)
 s=c.execute('SELECT * FROM settings WHERE id=1').fetchone(); now=int(time.time()); closed=bool(s['closed'] or (s['closes_at'] and now>=s['closes_at']))
 rows=c.execute('SELECT m.id,m.name,b.amount_cents,b.visibility,b.updated_at,b.id AS bid_id FROM bids b JOIN members m ON m.id=b.member_id ORDER BY b.updated_at ASC,b.id ASC').fetchall()
 winner=finalize_winner(c) if closed else None
 visible=[]
 for r in rows:
  own=member_id==r['id']; see=admin or own or r['visibility']=='public'
  visible.append({'name':r['name'],'amount':r['amount_cents']/100 if see else None,'visibility':r['visibility'],'mine':own,'member_id':r['id'] if admin else None,'updated_at':r['updated_at'] if see else None})
 return {'title':s['title'],'start':s['start_cents']/100,'increment':s['increment_cents']/100,'closes_at':s['closes_at'],'closed':closed,'started':bool(s['started']),'count':len(rows),'member_limit':s['member_limit'],'approved_count':c.execute('SELECT COUNT(*) FROM members WHERE approved=1').fetchone()[0],'members':[{'id':r['id'],'name':r['name'],'approved':bool(r['approved']),'has_bid':bool(r['has_bid'])} for r in c.execute('SELECT m.id,m.name,m.approved,EXISTS(SELECT 1 FROM bids b WHERE b.member_id=m.id) AS has_bid FROM members m ORDER BY m.id').fetchall()] if admin else [],'bids':visible,'winner':winner if closed else None,'admin':admin,'logged_in':bool(member_id),'my_name':c.execute('SELECT name FROM members WHERE id=?',(member_id,)).fetchone()['name'] if member_id else None,'approved':bool(c.execute('SELECT approved FROM members WHERE id=?',(member_id,)).fetchone()['approved']) if member_id else False,'archives':[{'id':r['id'],'title':r['title'],'archived_at':r['archived_at'],'winner':json.loads(r['winner_json']) if r['winner_json'] else None,'bid_count':len(json.loads(r['bids_json']))} for r in c.execute('SELECT * FROM auction_archive ORDER BY id DESC LIMIT 20').fetchall()] if admin else [],'roulette_entries':[dict(r) for r in c.execute('SELECT id,name,weight FROM roulette_entries ORDER BY id').fetchall()] if admin else [],'roulette_result':({'name':json.loads(s['roulette_result_json'])['name'],'drawn_at':json.loads(s['roulette_result_json']).get('drawn_at')} if not admin else json.loads(s['roulette_result_json'])) if s['roulette_result_json'] else None,'roulette_names':[r['name'] for r in c.execute('SELECT name FROM roulette_entries ORDER BY id').fetchall()],'roulette_history':[{'id':r['id'],'auction_title':r['auction_title'],'winner_name':r['winner_name'],'drawn_at':r['drawn_at'],'names':json.loads(r['names_json']) if r['names_json'] else []} for r in c.execute('SELECT id,auction_title,winner_name,drawn_at,names_json FROM roulette_history ORDER BY id DESC').fetchall()],'roulette_title':s['roulette_title'],'roulette_available':True,'roulette_starts_at':s['roulette_starts_at'],'roulette_ends_at':s['roulette_ends_at'],'server_now':int(time.time()),'pending_members':[dict(r) for r in c.execute('SELECT id,name FROM members WHERE approved=0 ORDER BY id').fetchall()] if admin else []}

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
  if path=='/api/public-roulette':
   # Deliberately public. Never include private weights or member credentials.
   with LOCK,conn() as c:
    reconcile_roulette_history(c)
    setting=c.execute('SELECT roulette_title,roulette_result_json FROM settings WHERE id=1').fetchone()
    rows=c.execute('SELECT id,auction_title,winner_name,drawn_at,names_json FROM roulette_history ORDER BY drawn_at DESC,id DESC').fetchall()
    history=[]
    for r in rows:
     try: names=json.loads(r['names_json']) if r['names_json'] else []
     except (ValueError,TypeError): names=[]
     history.append({'id':r['id'],'title':r['auction_title'],'winner':r['winner_name'],'drawn_at':r['drawn_at'],'names':names})
    # For a new spin, previous completed winner remains the public result.
    title=history[0]['title'] if history else setting['roulette_title']
    self.send(200,{'title':title,'latest':history[0] if history else None,'history':history}); return
  if path=='/api/state':
   with LOCK,conn() as c:
    m,a=self.auth(c); result=state(c,m,a)
   self.send(200,result); return
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
     limit=c.execute('SELECT member_limit FROM settings WHERE id=1').fetchone()['member_limit']
     if limit is not None and c.execute('SELECT COUNT(*) FROM members').fetchone()[0]>=limit: raise ValueError('Member capacity reached; contact organizer')
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
    if path=='/api/create-member':
     if not a: return self.send(403,{'error':'Organizer login required'})
     name=str(data.get('name','')).strip(); pin=str(data.get('pin',''))
     if not 2<=len(name)<=50 or not (pin.isdigit() and 4<=len(pin)<=12): raise ValueError('Username must be 2–50 characters and PIN must be 4–12 digits')
     limit=c.execute('SELECT member_limit FROM settings WHERE id=1').fetchone()['member_limit']
     if limit is not None and c.execute('SELECT COUNT(*) FROM members').fetchone()[0]>=limit: raise ValueError('Member capacity reached; increase total member limit first')
     c.execute('INSERT INTO members(name,pin_hash,approved) VALUES(?,?,1)',(name,pw_hash(pin)))
     return self.send(200,{'ok':True})
    if path=='/api/member-limit':
     if not a: return self.send(403,{'error':'Organizer login required'})
     limit=int(data.get('member_limit',0))
     if not 1<=limit<=10000: raise ValueError('Member limit must be between 1 and 10,000')
     existing=c.execute('SELECT COUNT(*) FROM members').fetchone()[0]
     if limit<existing: raise ValueError(f'Cannot set capacity below {existing} existing accounts')
     c.execute('UPDATE settings SET member_limit=? WHERE id=1',(limit,))
     return self.send(200,{'ok':True})
    if path=='/api/rename-member':
     if not a: return self.send(403,{'error':'Organizer login required'})
     member_id=int(data.get('member_id',0)); new_name=str(data.get('name','')).strip()
     if not 2<=len(new_name)<=50: raise ValueError('Username must be 2–50 characters')
     row=c.execute('SELECT name FROM members WHERE id=?',(member_id,)).fetchone()
     if not row: raise ValueError('Member not found')
     if c.execute('SELECT 1 FROM members WHERE name=? AND id<>?',(new_name,member_id)).fetchone(): raise ValueError('Username already taken')
     if row['name']!=new_name:
      has_bid=int(bool(c.execute('SELECT 1 FROM bids WHERE member_id=?',(member_id,)).fetchone()))
      c.execute('UPDATE members SET name=? WHERE id=?',(new_name,member_id))
      c.execute('INSERT INTO member_admin_audit(created_at,member_id,old_name,new_name,action,had_bid) VALUES(?,?,?,?,?,?)',(int(time.time()),member_id,row['name'],new_name,'rename',has_bid))
     return self.send(200,{'ok':True})
    if path=='/api/delete-member':
     if not a: return self.send(403,{'error':'Organizer login required'})
     member_id=int(data.get('member_id',0))
     row=c.execute('SELECT name FROM members WHERE id=?',(member_id,)).fetchone()
     if not row: raise ValueError('Member not found')
     s=c.execute('SELECT * FROM settings WHERE id=1').fetchone()
     if s['closed'] or (s['closes_at'] and time.time()>=s['closes_at']):
      raise ValueError('Auction is closed. Create a new round before deleting members to preserve the result.')
     bid=c.execute('SELECT * FROM bids WHERE member_id=?',(member_id,)).fetchone()
     if bid:
      c.execute('INSERT INTO bid_removals(removed_at,member_id,member_name,amount_cents,visibility,reason) VALUES(?,?,?,?,?,?)',(int(time.time()),member_id,row['name'],bid['amount_cents'],bid['visibility'],'Member account deleted by organizer'))
     c.execute('INSERT INTO member_admin_audit(created_at,member_id,old_name,new_name,action,had_bid) VALUES(?,?,?,?,?,?)',(int(time.time()),member_id,row['name'],None,'delete',int(bool(bid))))
     c.execute('DELETE FROM bids WHERE member_id=?',(member_id,))
     c.execute('DELETE FROM sessions WHERE member_id=?',(member_id,))
     c.execute('DELETE FROM members WHERE id=?',(member_id,))
     return self.send(200,{'ok':True})
    if path=='/api/reset-member-pin':
     if not a: return self.send(403,{'error':'Organizer login required'})
     member_id=int(data.get('member_id',0)); pin=str(data.get('pin',''))
     if not (pin.isdigit() and 4<=len(pin)<=12): raise ValueError('PIN must be 4–12 digits')
     cur=c.execute('UPDATE members SET pin_hash=? WHERE id=?',(pw_hash(pin),member_id))
     if not cur.rowcount: raise ValueError('Member not found')
     c.execute('DELETE FROM sessions WHERE member_id=?',(member_id,))
     return self.send(200,{'ok':True})
    if path=='/api/approval':
     if not a: return self.send(403,{'error':'Organizer login required'})
     member_id=int(data.get('member_id',0)); action=data.get('action')
     if action=='approve':
      limit=c.execute('SELECT member_limit FROM settings WHERE id=1').fetchone()['member_limit']
      if limit is not None and c.execute('SELECT COUNT(*) FROM members WHERE approved=1').fetchone()[0]>=limit: raise ValueError('Approved member capacity reached')
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
    if path=='/api/delete-archive':
     if not a: return self.send(403,{'error':'Organizer login required'})
     archive_id=int(data.get('archive_id',0))
     row=c.execute('SELECT id,title FROM auction_archive WHERE id=?',(archive_id,)).fetchone()
     if not row: raise ValueError('Archived auction not found')
     if str(data.get('confirm_title',''))!=row['title']: raise ValueError('Auction title confirmation does not match')
     c.execute('INSERT INTO archive_deletion_audit(archive_id,auction_title,deleted_at) VALUES(?,?,?)',(row['id'],row['title'],int(time.time())))
     c.execute('DELETE FROM auction_archive WHERE id=?',(archive_id,))
     return self.send(200,{'ok':True})
    if path=='/api/roulette-delete-all-history':
     if not a: return self.send(403,{'error':'Organizer login required'})
     c.execute('INSERT OR IGNORE INTO roulette_history_deleted(drawn_at,winner_name) SELECT drawn_at,winner_name FROM roulette_history')
     c.execute('DELETE FROM roulette_history')
     c.execute('UPDATE settings SET roulette_result_json=NULL WHERE id=1')
     return self.send(200,{'ok':True})
    if path=='/api/roulette-delete-history':
     if not a: return self.send(403,{'error':'Organizer login required'})
     rid=data.get('id')
     if type(rid) is not int: raise ValueError('Invalid history ID')
     row=c.execute('SELECT drawn_at,winner_name FROM roulette_history WHERE id=?',(rid,)).fetchone()
     if not row: raise ValueError('History record not found')
     c.execute('INSERT OR IGNORE INTO roulette_history_deleted(drawn_at,winner_name) VALUES(?,?)',(row['drawn_at'],row['winner_name']))
     c.execute('DELETE FROM roulette_history WHERE id=?',(rid,))
     setting=c.execute('SELECT roulette_result_json FROM settings WHERE id=1').fetchone()
     if setting and setting['roulette_result_json']:
      try:
       current=json.loads(setting['roulette_result_json'])
       if current.get('name')==row['winner_name'] and current.get('drawn_at')==row['drawn_at']:
        c.execute('UPDATE settings SET roulette_result_json=NULL WHERE id=1')
      except (ValueError,TypeError): pass
     return self.send(200,{'ok':True})
    if path=='/api/roulette-title':
     if not a: return self.send(403,{'error':'Organizer login required'})
     title=str(data.get('title','')).strip()
     if not 1<=len(title)<=100: raise ValueError('Title must be 1–100 characters')
     if c.execute('SELECT roulette_result_json FROM settings WHERE id=1').fetchone()['roulette_result_json']: raise ValueError('Start a new spin to change title')
     c.execute('UPDATE settings SET roulette_title=? WHERE id=1',(title,))
     return self.send(200,{'ok':True})
    if path=='/api/roulette-new-round':
     if not a: return self.send(403,{'error':'Organizer login required'})
     s=c.execute('SELECT roulette_result_json FROM settings WHERE id=1').fetchone()
     if not s['roulette_result_json']: raise ValueError('Complete the current roulette draw before starting a new round')
     c.execute('DELETE FROM roulette_entries')
     c.execute("UPDATE settings SET roulette_result_json=NULL,roulette_title='Casino Roulette',roulette_starts_at=NULL,roulette_ends_at=NULL WHERE id=1")
     return self.send(200,{'ok':True})
    if path=='/api/roulette-schedule':
     if not a: return self.send(403,{'error':'Organizer login required'})
     def stamp(value):
      if value in (None,''): return None
      if type(value) is not int or not 0<value<4102444800: raise ValueError('Invalid schedule timestamp')
      return value
     start=stamp(data.get('starts_at')); end=stamp(data.get('ends_at'))
     if start is not None and end is not None and end<=start: raise ValueError('End must be after start')
     c.execute('UPDATE settings SET roulette_starts_at=?,roulette_ends_at=? WHERE id=1',(start,end))
     return self.send(200,{'ok':True})
    if path=='/api/roulette-entries':
     if not a: return self.send(403,{'error':'Organizer login required'})
     s=c.execute('SELECT * FROM settings WHERE id=1').fetchone()
     if s['roulette_result_json']: raise ValueError('Draw is finalized; create a new auction to change entries')
     entries=data.get('entries')
     if not isinstance(entries,list) or not 1<=len(entries)<=100: raise ValueError('Enter 1 to 100 names')
     cleaned=[]; names=set()
     for entry in entries:
      name=str(entry.get('name','')).strip(); weight=entry.get('weight')
      if not 1<=len(name)<=60 or name.casefold() in names: raise ValueError('Names must be unique and 1–60 characters')
      if type(weight) is not int or not 0<=weight<=10000: raise ValueError('Percentage must be between 0 and 100, with at most 2 decimal places')
      cleaned.append((name,weight)); names.add(name.casefold())
     if sum(weight for _,weight in cleaned)!=10000: raise ValueError('Percentages must add up to exactly 100%')
     c.execute('DELETE FROM roulette_entries')
     c.executemany('INSERT INTO roulette_entries(name,weight) VALUES(?,?)',cleaned)
     return self.send(200,{'ok':True})
    if path=='/api/roulette-spin':
     if not a: return self.send(403,{'error':'Organizer login required'})
     s=c.execute('SELECT * FROM settings WHERE id=1').fetchone()
     if s['roulette_result_json']: return self.send(200,{'result':json.loads(s['roulette_result_json'])})
     entries=c.execute('SELECT name,weight FROM roulette_entries ORDER BY id').fetchall()
     if not entries: raise ValueError('Add participants first')
     total=sum(e['weight'] for e in entries)
     if total<=0: raise ValueError('At least one participant must have a positive percentage')
     ticket=secrets.randbelow(total)
     selected=entries[-1]
     for e in entries:
      ticket-=e['weight']
      if ticket<0: selected=e; break
     result={'name':selected['name'],'weight':selected['weight'],'total_weight':total,'drawn_at':int(time.time())}
     c.execute('UPDATE settings SET roulette_result_json=? WHERE id=1',(json.dumps(result),))
     c.execute('INSERT INTO roulette_history(auction_title,winner_name,drawn_at,names_json) VALUES(?,?,?,?)',(s['roulette_title'],result['name'],result['drawn_at'],json.dumps([e['name'] for e in entries])))
     return self.send(200,{'result':result})
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
     winner=finalize_winner(c)
     c.execute('INSERT INTO auction_archive(archived_at,title,start_cents,increment_cents,closes_at,closed,bids_json,winner_json) VALUES(?,?,?,?,?,?,?,?)',(int(time.time()),old['title'],old['start_cents'],old['increment_cents'],old['closes_at'],int(bool(old['closed'] or (old['closes_at'] and time.time()>=old['closes_at']))),json.dumps(history),json.dumps(winner) if winner else (old['roulette_result_json'] if old['roulette_result_json'] else None)))
     c.execute('DELETE FROM bids')
     c.execute('DELETE FROM roulette_entries')
     c.execute('UPDATE settings SET roulette_result_json=NULL,roulette_starts_at=NULL,roulette_ends_at=NULL,title=?,start_cents=?,increment_cents=?,closes_at=?,closed=0,started=0,winner_json=NULL WHERE id=1',(title,start,increment,close))
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
     c.execute('UPDATE settings SET closed=1 WHERE id=1'); finalize_winner(c); return self.send(200,{'ok':True})
    return self.send(404,{'error':'Unknown action'})
  except (ValueError,TypeError,KeyError,OverflowError) as e: return self.send(400,{'error':str(e)})
  except sqlite3.IntegrityError: return self.send(400,{'error':'Name already taken'})

if __name__=='__main__':
 init()
 if not ADMIN_PASSWORD: print('WARNING: organizer login disabled until AUCTION_ADMIN_PASSWORD is set')
 print(f'Open http://localhost:{PORT}')
 ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
