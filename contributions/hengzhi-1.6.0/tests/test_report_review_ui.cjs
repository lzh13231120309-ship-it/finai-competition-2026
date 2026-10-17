const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../web/app.js'),'utf8');
function render(run){
 const dialog={innerHTML:'',open:false,showModal(){this.open=true}},close={};
 const ctx=vm.createContext({S:{currentTask:'task'},$:s=>s==='#finance-dialog'?dialog:close,esc:s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),financialTable:r=>JSON.stringify(r)});
 vm.runInContext(source.slice(source.indexOf('function showReportReview('),source.indexOf('async function reportReview(')),ctx);ctx.run=run;vm.runInContext('showReportReview(run)',ctx);return dialog;
}
test('missing total is not presented as full score',()=>{
 const d=render({run_id:'r',claim_review:{stats:{unverifiable:3}},quality_review:{overall_total:null,assessed_weight:4,provisional_score:100,dimensions:[]}});
 assert.match(d.innerHTML,/待人工评审/);assert.match(d.innerHTML,/只代表已评范围/);assert.match(d.innerHTML,/4\/100/);assert.equal(d.open,true);
});
test('unsafe text is escaped and download urls are generated locally',()=>{
 const d=render({run_id:'r',summary:'<script>alert(1)</script>',exports:[{url:'javascript:alert(1)'}],quality_review:{delivery_blocked:true}});
 assert.doesNotMatch(d.innerHTML,/<script>/);assert.doesNotMatch(d.innerHTML,/javascript:/);assert.match(d.innerHTML,/存在需要修订/);
});
