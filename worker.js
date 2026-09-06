const json=(data,status=200)=>new Response(JSON.stringify(data),{status,headers:{'content-type':'application/json','cache-control':'no-store'}});
const now=()=>new Date().toISOString();
function auth(request,env){return env.STORAGE_API_KEY&&request.headers.get('X-Storage-Key')===env.STORAGE_API_KEY;}
function cleanName(name){return String(name||'file.bin').replace(/[^A-Za-z0-9._ -]/g,'_').slice(0,160)||'file.bin';}
function email(value){return String(value||'').trim().toLowerCase();}
async function upload(request,env){
  const d=await request.json(); const owner=email(d.ownerEmail); const raw=String(d.fileBase64||'').replace(/^data:[^;]+;base64,/i,'');
  if(!owner||!raw)return json({success:false,error:'ownerEmail and fileBase64 are required'},400);
  const bin=Uint8Array.from(atob(raw),c=>c.charCodeAt(0)); if(bin.byteLength>52428800)return json({success:false,error:'File is too large'},413);
  const id=crypto.randomUUID(),name=cleanName(d.fileName),key=`users/${btoa(owner).replace(/[^A-Za-z0-9]/g,'').slice(0,40)}/${id}-${name}`,created=now(),expires=new Date(Date.now()+Number(env.RETENTION_DAYS||15)*86400000).toISOString();
  await env.FILES.put(key,bin,{httpMetadata:{contentType:String(d.mimeType||'application/octet-stream').slice(0,120)}});
  await env.DB.prepare('INSERT INTO files(id,owner_email,office_name,original_name,storage_key,mime_type,size_bytes,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)').bind(id,owner,String(d.officeName||'').slice(0,200),name,key,String(d.mimeType||'application/octet-stream').slice(0,120),bin.byteLength,created,expires).run();
  return json({success:true,fileId:id,expiresAt:expires});
}
async function download(request,env){
  const d=await request.json(),owner=email(d.ownerEmail),row=await env.DB.prepare('SELECT * FROM files WHERE id=? AND owner_email=? AND deleted_at IS NULL AND expires_at>?').bind(String(d.fileId),owner,now()).first();
  if(!row)return json({success:false,error:'File not found or expired'},404); const max=Number(env.MAX_DOWNLOADS||3); if(row.download_count>=max)return json({success:false,error:'Download limit reached'},429);
  const object=await env.FILES.get(row.storage_key); if(!object)return json({success:false,error:'Stored file unavailable'},404);
  await env.DB.batch([env.DB.prepare('UPDATE files SET download_count=download_count+1 WHERE id=?').bind(row.id),env.DB.prepare('INSERT INTO download_history(file_id,owner_email,downloaded_at,user_agent) VALUES(?,?,?,?)').bind(row.id,owner,now(),request.headers.get('User-Agent')||'')]);
  const headers=new Headers({'content-type':row.mime_type,'content-disposition':`attachment; filename="${row.original_name.replace(/"/g,'')}"`,'cache-control':'private, no-store'}); object.writeHttpMetadata(headers); headers.set('x-downloads-remaining',String(max-row.download_count-1)); return new Response(object.body,{headers});
}
async function history(request,env){const d=await request.json(),owner=email(d.ownerEmail); const {results}=await env.DB.prepare('SELECT id,original_name,mime_type,size_bytes,created_at,expires_at,download_count FROM files WHERE owner_email=? AND deleted_at IS NULL ORDER BY created_at DESC').bind(owner).all(); return json({success:true,files:results});}
async function cleanup(env){const {results}=await env.DB.prepare('SELECT id,storage_key FROM files WHERE expires_at<=? AND deleted_at IS NULL').bind(now()).all(); for(const row of results){await env.FILES.delete(row.storage_key);await env.DB.prepare('UPDATE files SET deleted_at=? WHERE id=?').bind(now(),row.id).run();} return results.length;}
export default {async fetch(request,env){try{const url=new URL(request.url);if(request.method==='GET'&&url.pathname==='/health')return json({success:true,service:'d1-storage-worker'});if(!auth(request,env))return json({success:false,error:'Unauthorized'},401);if(request.method==='POST'&&url.pathname==='/upload')return upload(request,env);if(request.method==='POST'&&url.pathname==='/download')return download(request,env);if(request.method==='POST'&&url.pathname==='/history')return history(request,env);return json({success:false,error:'Not found'},404);}catch(e){console.error(e);return json({success:false,error:'Storage operation failed'},500);}},async scheduled(event,env,ctx){ctx.waitUntil(cleanup(env));}};
