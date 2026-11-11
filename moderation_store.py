"""Additive SQLite migrations and durable moderation records."""
import json
from datetime import datetime, timezone, timedelta
import uuid

RULES_VERSION = 'moderator-2.0.0'
def now(): return datetime.now(timezone.utc).isoformat()

def migrate(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS member_profiles(chat_id INTEGER,user_id INTEGER,first_seen TEXT,PRIMARY KEY(chat_id,user_id));
    CREATE TABLE IF NOT EXISTS ai_settings(chat_id INTEGER PRIMARY KEY,mode TEXT DEFAULT 'observation');
    CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY,chat_id INTEGER,user_id INTEGER,message_id INTEGER,
      category TEXT,confidence REAL,reason TEXT,evidence TEXT,source_text TEXT,context TEXT,
      mode TEXT,recommended TEXT,action TEXT DEFAULT 'none',review_status TEXT DEFAULT 'pending',
      rules_version TEXT,model_version TEXT,created_at TEXT,reviewer INTEGER,reviewed_at TEXT,error TEXT);
    CREATE UNIQUE INDEX IF NOT EXISTS decision_message ON decisions(chat_id,message_id);
    CREATE INDEX IF NOT EXISTS decision_chat ON decisions(chat_id,created_at);
    CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY,chat_id INTEGER,user_id INTEGER,actor INTEGER,
      action TEXT,created_at TEXT,decision_id TEXT,details TEXT);
    CREATE TABLE IF NOT EXISTS restrictions(chat_id INTEGER,user_id INTEGER,kind TEXT,until_at TEXT,PRIMARY KEY(chat_id,user_id));
    INSERT OR IGNORE INTO member_profiles SELECT chat_id,user_id,NULL FROM chat_users;
    PRAGMA user_version=2;
    ''');db.commit()

def mode(db,cid):
    row=db.execute('SELECT mode FROM ai_settings WHERE chat_id=?',(cid,)).fetchone()
    return row[0] if row else 'observation'

def audit(db,cid,uid,actor,action,did=None,details=''):
    db.execute('INSERT INTO audit_log(chat_id,user_id,actor,action,created_at,decision_id,details) VALUES(?,?,?,?,?,?,?)',
               (cid,uid,actor,action,now(),did,details));db.commit()

def record(db,cid,uid,mid,text,context,result,current_mode):
    did=uuid.uuid4().hex[:12]
    db.execute('''INSERT OR IGNORE INTO decisions(id,chat_id,user_id,message_id,category,confidence,reason,evidence,
      source_text,context,mode,recommended,rules_version,model_version,created_at,review_status)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
      (did,cid,uid,mid,result['category'],result['confidence'],result['reason'],result['evidence'],text[:4000],
       json.dumps(context,ensure_ascii=False),current_mode,result['action'],RULES_VERSION,result['model'],now(),
       'pending' if result['action']!='pass' else 'passed'))
    db.commit()
    return db.execute('SELECT id FROM decisions WHERE chat_id=? AND message_id=?',(cid,mid)).fetchone()[0]

def get(db,did):
    cur=db.execute('SELECT * FROM decisions WHERE id=?',(did,));row=cur.fetchone()
    return dict(zip([c[0] for c in cur.description],row)) if row else None

def purge(db,days):
    cutoff=(datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
    db.execute("UPDATE decisions SET source_text='',context='',evidence='' WHERE created_at<?",(cutoff,));db.commit()
