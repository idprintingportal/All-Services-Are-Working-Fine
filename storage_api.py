import base64, hashlib, os, re, uuid
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, request
import boto3
import psycopg
from psycopg.rows import dict_row

app=Flask(__name__)
DB=os.environ['DATABASE_URL']; API_KEY=os.environ['STORAGE_API_KEY']; BUCKET=os.environ['R2_BUCKET']; RETENTION=int(os.getenv('RETENTION_DAYS','15')); MAX_DOWNLOADS=int(os.getenv('MAX_DOWNLOADS','3'))
s3=boto3.client('s3',endpoint_url=os.environ['R2_ENDPOINT'],aws_access_key_id=os.environ['R2_ACCESS_KEY'],aws_secret_access_key=os.environ['R2_SECRET_KEY'],region_name='auto')

def db(): return psycopg.connect(DB,row_factory=dict_row)
def auth(): return request.headers.get('X-Storage-Key')==API_KEY
def init_db():
  with db() as c:
    c.execute('''CREATE TABLE IF NOT EXISTS files(id UUID PRIMARY KEY,owner_email TEXT NOT NULL,office_name TEXT,original_name TEXT NOT NULL,storage_key TEXT UNIQUE NOT NULL,mime_type TEXT NOT NULL,size_bytes BIGINT NOT NULL,sha256 TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT now(),expires_at TIMESTAMPTZ NOT NULL,download_count INT NOT NULL DEFAULT 0,deleted_at TIMESTAMPTZ)''')
    c.execute('''CREATE TABLE IF NOT EXISTS download_history(id BIGSERIAL PRIMARY KEY,file_id UUID NOT NULL,owner_email TEXT NOT NULL,downloaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),ip_hash TEXT,user_agent TEXT)''')
init_db()

@app.get('/health')
def health(): return jsonify(success=True,service='storage-api')

@app.post('/files/upload')
def upload():
  if not auth(): return jsonify(success=False,error='Unauthorized'),401
  data=request.get_json(silent=True) or {}; raw=base64.b64decode(str(data.get('fileBase64','')),validate=True) if data.get('fileBase64') else b''
  email=str(data.get('ownerEmail','')).strip().lower(); name=re.sub(r'[^A-Za-z0-9._ -]','_',str(data.get('fileName','file.bin')))[:160]
  if not raw or not email: return jsonify(success=False,error='ownerEmail and fileBase64 are required'),400
  if len(raw)>50*1024*1024: return jsonify(success=False,error='File is too large'),413
  fid=str(uuid.uuid4()); key=f'users/{hashlib.sha256(email.encode()).hexdigest()}/{fid}-{name}'; now=datetime.now(timezone.utc); exp=now+timedelta(days=RETENTION); digest=hashlib.sha256(raw).hexdigest(); mime=str(data.get('mimeType','application/octet-stream'))[:120]
  s3.put_object(Bucket=BUCKET,Key=key,Body=raw,ContentType=mime,ServerSideEncryption='AES256')
  try:
    with db() as c: c.execute('INSERT INTO files VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,NULL)',(fid,email,str(data.get('officeName',''))[:200],name,key,mime,len(raw),digest,now,exp))
  except Exception:
    s3.delete_object(Bucket=BUCKET,Key=key); raise
  return jsonify(success=True,fileId=fid,expiresAt=exp.isoformat())

@app.post('/files/download')
def download():
  if not auth(): return jsonify(success=False,error='Unauthorized'),401
  data=request.get_json(silent=True) or {}; fid=str(data.get('fileId','')); email=str(data.get('ownerEmail','')).strip().lower()
  with db() as c:
    row=c.execute('SELECT * FROM files WHERE id=%s AND owner_email=%s AND deleted_at IS NULL AND expires_at>now() FOR UPDATE',(fid,email)).fetchone()
    if not row: return jsonify(success=False,error='File not found or expired'),404
    if row['download_count']>=MAX_DOWNLOADS: return jsonify(success=False,error='Download limit reached'),429
    c.execute('UPDATE files SET download_count=download_count+1 WHERE id=%s',(fid,)); c.execute('INSERT INTO download_history(file_id,owner_email,ip_hash,user_agent) VALUES(%s,%s,%s,%s)',(fid,email,hashlib.sha256(request.remote_addr.encode()).hexdigest() if request.remote_addr else '',request.headers.get('User-Agent','')[:300]))
  url=s3.generate_presigned_url('get_object',Params={'Bucket':BUCKET,'Key':row['storage_key']},ExpiresIn=300)
  return jsonify(success=True,url=url,downloadsRemaining=MAX_DOWNLOADS-row['download_count']-1)

@app.post('/files/history')
def history():
  if not auth(): return jsonify(success=False,error='Unauthorized'),401
  email=str((request.get_json(silent=True) or {}).get('ownerEmail','')).strip().lower()
  with db() as c: rows=c.execute('SELECT id,original_name,mime_type,size_bytes,created_at,expires_at,download_count FROM files WHERE owner_email=%s AND deleted_at IS NULL ORDER BY created_at DESC',(email,)).fetchall()
  return jsonify(success=True,files=rows)

if __name__=='__main__': init_db(); app.run(host='0.0.0.0',port=int(os.getenv('PORT','10000')))
