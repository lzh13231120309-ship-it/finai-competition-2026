const test=require('node:test'), assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../web/app.js'),'utf8');
function render(run){
 const dialog={innerHTML:'',open:false,showModal(){this.open=true}},close={};
 const ctx=vm.createContext({S:{currentTask:'task'},$:s=>s==='#finance-dialog'?dialog:close,
 esc:s=>String(s??'').replace(/</g,'&lt;'),financialLabel:s=>s,
 financialTable:(rows)=>JSON.stringify(rows),valuationNumber:(v,u)=>v==null?'—':String(v)+' '+u});
 vm.runInContext(source.slice(source.indexOf('function showValuationModel('),source.indexOf('async function valuationModel(')),ctx);
 ctx.run=run;vm.runInContext('showValuationModel(run)',ctx);return dialog;
}
test('canonical model with complete_result URL still renders values and sources',()=>{
 const d=render({kind:'valuation_model',run_id:'r',complete_result:'/api/run.json',ok:true,
 model_plan:{method:'multi_stage_fcff',currency:'CNY'},discount_rate:{wacc:'.1',method:'CAPM'},
 scenarios:{base:{valuation:{ok:true,equity_value:'12345',value_per_share:null}}},
 assumption_sources:{revenue_base:{source:'fact'}},exports:[{format:'xlsx',url:'/api/financial/artifacts/task/r/报告.xlsx'}]});
 assert.match(d.innerHTML,/12345/);assert.match(d.innerHTML,/财报取数/);assert.match(d.innerHTML,/有来源的市场参数/);assert.match(d.innerHTML,/可编辑 Excel/);assert.equal(d.open,true);
});
test('blocked diagnostics and unsafe download URLs are handled',()=>{
 const d=render({run_id:'r',ok:false,model_plan:{status:'blocked'},warnings:['缺少总股本'],
 exports:[{format:'xlsx',url:'javascript:alert(1)'}]});
 assert.match(d.innerHTML,/缺少总股本/);assert.match(d.innerHTML,/尚需补充/);assert.doesNotMatch(d.innerHTML,/javascript:/);
});
test('review conditions and peer comparisons are visible in the result dialog',()=>{
 const d=render({run_id:'r',ok:true,valuation_review:{summary:'结论需核验',reasons:['净债务为未核验假设'],method_comparisons:[{method:'pe',sample_count:4,equity_value_median:'500',relative_gap:'1'}]}});
 assert.match(d.innerHTML,/结论需核验/);assert.match(d.innerHTML,/净债务为未核验假设/);assert.match(d.innerHTML,/equity_value_median/);assert.match(d.innerHTML,/账面净资产不是价值底线/);
});
