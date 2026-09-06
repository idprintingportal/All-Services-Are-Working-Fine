import os, boto3, psycopg
from psycopg.rows import tuple_row
s3=boto3.client('s3',endpoint_url=os.environ['R2_ENDPOINT'],aws_access_key_id=os.environ['R2_ACCESS_KEY'],aws_secret_access_key=os.environ['R2_SECRET_KEY'],region_name='auto')
with psycopg.connect(os.environ['DATABASE_URL'],row_factory=tuple_row) as c:
  rows=c.execute("SELECT id,storage_key FROM files WHERE expires_at<=now() AND deleted_at IS NULL").fetchall()
  for fid,key in rows:
    try: s3.delete_object(Bucket=os.environ['R2_BUCKET'],Key=key)
    except Exception: continue
    c.execute('UPDATE files SET deleted_at=now() WHERE id=%s',(fid,))
  print(f'cleaned={len(rows)}')
