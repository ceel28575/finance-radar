export const blocked=x=>/奇幻团课|qmxp2077/i.test([x.source,x.room,x.author,x.room_key].join(' '));
export function dayOf(t){return new Date(Date.parse(t)+8*3600000).toISOString().slice(0,10)}
const str=(v,n)=>typeof v==='string'&&v.trim().length>0&&v.length<=n;
export function normalizeItem(x){
 if(!x||typeof x!=='object'||!str(x.id||x.source_key,2000)||!str(x.content,100000)||!str(x.published_at,60)||!/(Z|[+-]\d{2}:\d{2})$/.test(x.published_at)||!Number.isFinite(Date.parse(x.published_at)))throw Error('资讯缺少有效 ID、正文或带时区的时间');
 if(blocked(x))return null;
 const room=x.room||x.room_name||x.source;
 if(!str(room,300))throw Error('资讯缺少来源');
 const result={...x,id:x.id||x.source_key,source:room,room,published_at:new Date(x.published_at).toISOString()};
 if(JSON.stringify(result).length>150000)throw Error('单条资讯过大');
 return result;
}
export function normalizeReport(r){
 if(!r||!/^\d{4}-\d{2}-\d{2}$/.test(r.date)||!Number.isFinite(Date.parse(r.generated_at))||!Array.isArray(r.stocks)||r.stocks.length>300)throw Error('报告格式错误');
 if(r.bull_probability!=null&&(!Number.isFinite(r.bull_probability)||r.bull_probability<0||r.bull_probability>100))throw Error('概率超出范围');
 for(const s of r.stocks)if(!str(s.name,100)||/\d{6}/.test(s.name)||!['bull','bear'].includes(s.direction)||!str(s.reason,20000))throw Error('股票池名称、方向或理由错误');
 if(JSON.stringify(r).length>250000)throw Error('报告过大');
 return r;
}
export function normalizeAIStatus(s){
 if(!s||typeof s!=='object'||Array.isArray(s))throw Error('AI 状态格式错误');
 if(!['ready','processing','degraded','disabled'].includes(s.status))throw Error('AI 状态值错误');
 if(!str(s.message,240)||!Number.isFinite(Date.parse(s.last_run_at)))throw Error('AI 状态缺少有效消息或时间');
 const result={
  status:s.status,
  message:s.message,
  last_run_at:s.last_run_at,
 };
 for(const key of ['provider','qwen_model','doubao_model','last_report_at']){
  if(s[key]!=null){
   if(typeof s[key]!=='string'||s[key].length>160)throw Error('AI 状态字段长度错误');
   result[key]=s[key];
  }
 }
 if(s.ocr_available!=null){
  if(typeof s.ocr_available!=='boolean')throw Error('OCR 状态格式错误');
  result.ocr_available=s.ocr_available;
 }
 if(s.ollama_gpu_status!=null){
  if(!['gpu','cpu','not_loaded','unavailable'].includes(s.ollama_gpu_status))throw Error('Ollama GPU 状态格式错误');
  result.ollama_gpu_status=s.ollama_gpu_status;
 }
 if(s.ollama_vram_bytes!=null){
  if(!Number.isFinite(s.ollama_vram_bytes)||s.ollama_vram_bytes<0||s.ollama_vram_bytes>1e13)throw Error('Ollama VRAM 数值错误');
  result.ollama_vram_bytes=s.ollama_vram_bytes;
 }
 return result;
}
export function normalizeBatch(body){
 if(!body||!Array.isArray(body.items)||body.items.length>200||!Array.isArray(body.reports||[])||(body.reports||[]).length>20)throw Error('每批最多 200 条资讯、20 份报告');
 const items=body.items.map(normalizeItem).filter(Boolean),reports=(body.reports||[]).map(normalizeReport);
 const ai_status=body.ai_status==null?null:normalizeAIStatus(body.ai_status);
 return {items,reports,ai_status,filtered:body.items.length-items.length};
}
