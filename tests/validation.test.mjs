import test from 'node:test';import assert from 'node:assert/strict';
import {normalizeBatch,normalizeAIStatus,dayOf} from '../lib/radar-validation.mjs';
const item={id:'a',source:'MX',room:'测试房间',content:'正文',published_at:'2026-10-26T22:15:00Z'};
test('CST archive and room source',()=>{const x=normalizeBatch({items:[item]});assert.equal(x.items[0].source,'测试房间');assert.equal(dayOf(x.items[0].published_at),'2026-10-27')});
test('blocked sources excluded server side',()=>assert.equal(normalizeBatch({items:[{...item,author:'qmxp2077'}]}).items.length,0));
test('invalid or naive dates rejected',()=>{for(const stamp of ['bad','2026-10-27T09:00:00'])assert.throws(()=>normalizeBatch({items:[{...item,published_at:stamp}]}))});
test('overlarge batch rejected',()=>assert.throws(()=>normalizeBatch({items:Array(201).fill(item)})));

test('AI pipeline status is validated and retained',()=>{
 const status={status:'processing',message:'千问队列尚余 12 条',last_run_at:'2026-10-30T15:00:00+08:00',qwen_model:'qwen3.5:4b',ocr_available:true,ollama_gpu_status:'gpu',ollama_vram_bytes:2147483648};
 const normalized=normalizeBatch({items:[],reports:[],ai_status:status});
 assert.equal(normalized.ai_status.status,'processing');
 assert.equal(normalized.ai_status.qwen_model,'qwen3.5:4b');
 assert.equal(normalized.ai_status.ocr_available,true);
 assert.equal(normalized.ai_status.ollama_gpu_status,'gpu');
 assert.equal(normalized.ai_status.ollama_vram_bytes,2147483648);
});
test('invalid AI pipeline status is rejected',()=>{
 assert.throws(()=>normalizeAIStatus({status:'unknown',message:'x',last_run_at:'bad'}));
});
