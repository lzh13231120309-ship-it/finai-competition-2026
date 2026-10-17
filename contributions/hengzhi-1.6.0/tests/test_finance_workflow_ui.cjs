const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../web/app.js'),'utf8');
function render(run){
 const dialog={innerHTML:'',open:false,showModal(){this.open=true}},close={};
 const context=vm.createContext({S:{currentTask:'t'},$:s=>s==='#finance-dialog'?dialog:close,esc:s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;')});
 vm.runInContext(source.slice(source.indexOf('function showFinanceWorkflow('),source.indexOf('async function financeWorkflow(')),context);context.run=run;vm.runInContext('showFinanceWorkflow(run)',context);return dialog;
}
test('missing evidence is visibly incomplete and unsupported links are excluded',()=>{
 const d=render({run_id:'r',completed:false,stages:[{name:'估值',status:'needs_input',summary:'缺少参数',refusals:['股数'],exports:[{url:'javascript:alert(1)',format:'xlsx'}]}]});
 assert.match(d.innerHTML,/待补充材料/);assert.match(d.innerHTML,/股数/);assert.doesNotMatch(d.innerHTML,/javascript:/);assert.ok(d.open);
});
test('quality score never hides its coverage or pretends to be full quality',()=>{
 const d=render({run_id:'r',stages:[{name:'质量',status:'needs_review',observable_score:4.5,observable_coverage:.5,summary:'<script>x</script>'}]});
 assert.match(d.innerHTML,/4.5/);assert.match(d.innerHTML,/覆盖率：0.5/);assert.match(d.innerHTML,/完整人工质量总分：待评/);assert.doesNotMatch(d.innerHTML,/<script>/);
});
