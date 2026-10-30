import { env } from 'cloudflare:workers';
import { normalizeBatch, dayOf } from '../../../../lib/radar-validation.mjs';
export const dynamic='force-dynamic';
const json=(x:unknown,status=200)=>Response.json(x,{status,headers:{'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});
const db=()=>{if(!env.DB)throw Error('数据库未连接');return env.DB};
function sameOrigin(req:Request){const origin=req.headers.get('origin');return !origin||origin===new URL(req.url).origin}
export async function GET(req:Request){
 try{
 const url=new URL(req.url),action=url.pathname.split('/').pop();
 // The entire site is owner-private; dispatch authenticates browser and service calls.
 if(action==='collector-config'){
  if(!req.headers.get('oai-authenticated-user-id'))return json({error:'请在网页中登录后下载'},401);
  if(!env.COLLECTOR_SITE_TOKEN)return json({error:'同步凭据尚未配置'},503);
  return new Response(JSON.stringify({version:1,site_url:url.origin,site_token:env.COLLECTOR_SITE_TOKEN},null,2),{headers:{'Content-Type':'application/json','Content-Disposition':'attachment; filename="cloud-config.json"','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});
 }
 if(action==='status'){
  const days=await db().prepare('SELECT day,COUNT(*) count FROM radar_items GROUP BY day ORDER BY day DESC LIMIT 366').all();
  const states=await db().prepare('SELECT key,value FROM radar_state').all<{key:string,value:string}>();
  const ai=await db().prepare('SELECT COUNT(*) AS count,MAX(generated_at) AS latest FROM radar_reports').first<{count:number,latest:string|null}>();
  const state=Object.fromEntries(states.results.map(x=>[x.key,JSON.parse(x.value)]));
  return json({days:days.results,state,ai_configured:Number(ai?.count||0)>0,latest_ai_report:ai?.latest||null,ocr_configured:state.ai_pipeline?.ocr_available===true});
 }
 if(action==='data'){
  const day=url.searchParams.get('day')||dayOf(new Date().toISOString());if(!/^\d{4}-\d{2}-\d{2}$/.test(day))return json({error:'日期错误'},400);
  const cursor=url.searchParams.get('cursor')||'';
  const rows=await db().prepare('SELECT id,payload FROM radar_items WHERE day=? AND id>? ORDER BY id LIMIT 501').bind(day,cursor).all<{id:string,payload:string}>();
  const page=rows.results.slice(0,500);
  const rs=cursor?[]:(await db().prepare('SELECT payload FROM radar_reports WHERE day=? ORDER BY generated_at DESC LIMIT 500').bind(day).all<{payload:string}>()).results;
  return json({items:page.map(x=>JSON.parse(x.payload)),reports:rs.map(x=>JSON.parse(x.payload)),next_cursor:rows.results.length>500?page.at(-1)?.id:null});
 }
 return json({error:'接口不存在'},404);
 }catch(e){console.error('radar GET failed',e instanceof Error?e.name:'Error');return json({error:'数据读取失败，请稍后重试'},503)}
}
export async function POST(req:Request){
 if(!sameOrigin(req))return json({error:'不接受跨站写入'},403);
 if(!req.headers.get('content-type')?.includes('application/json'))return json({error:'需要 JSON'},415);
 if(new URL(req.url).pathname.split('/').pop()!=='ingest')return json({error:'接口不存在'},404);
 try{
  // Stream bound prevents a misleading/missing Content-Length from allocating an unbounded body.
  const reader=req.body?.getReader();if(!reader)return json({error:'缺少数据'},400);
  const chunks:Uint8Array[]=[];let size=0;
  while(true){const {done,value}=await reader.read();if(done)break;size+=value.length;if(size>5_000_000){await reader.cancel();return json({error:'单次请求上限 5 MB'},413)}chunks.push(value)}
  const bytes=new Uint8Array(size);let offset=0;for(const c of chunks){bytes.set(c,offset);offset+=c.length}
  let batch,body;try{body=JSON.parse(new TextDecoder().decode(bytes));batch=normalizeBatch(body)}catch(e){return json({error:e instanceof Error?e.message:'数据格式错误'},400)}
  const now=new Date().toISOString();
  const statements=batch.items.map((x:any)=>db().prepare('INSERT INTO radar_items(id,day,room,published_at,payload,received_at) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET day=excluded.day,room=excluded.room,published_at=excluded.published_at,payload=excluded.payload,received_at=excluded.received_at').bind(x.id,dayOf(x.published_at),x.room,x.published_at,JSON.stringify(x),now));
  for(const r of batch.reports)statements.push(db().prepare('INSERT INTO radar_reports(id,day,generated_at,payload) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload').bind(r.date+'|'+r.generated_at,r.date,r.generated_at,JSON.stringify(r)));
  const status={at:now,accepted:batch.items.length,filtered:batch.filtered,client:body.client==='collector'?'collector':'import'};
  statements.push(db().prepare('INSERT INTO radar_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value').bind(status.client==='collector'?'collector':'last_import',JSON.stringify(status)));
  if(batch.ai_status){
   statements.push(db().prepare('INSERT INTO radar_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value').bind(
    'ai_pipeline',JSON.stringify({...batch.ai_status,at:now})
   ));
  }
  if(batch.collector_status){
   statements.push(db().prepare('INSERT INTO radar_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value').bind(
    'collector_scan',JSON.stringify({...batch.collector_status,at:now})
   ));
  }
  await db().batch(statements);
  return json({
   ok:true,
   accepted_ids:batch.items.map((x:any)=>x.id),
   filtered:batch.filtered,
   reports:batch.reports.length,
   ai_status_saved:!!batch.ai_status,
   collector_status_saved:!!batch.collector_status,
   received_at:now
  });
 }catch(e){console.error('radar ingest failed',e instanceof Error?e.name:'Error');return json({error:'保存失败；本机数据保留，请稍后重试'},503)}
}
