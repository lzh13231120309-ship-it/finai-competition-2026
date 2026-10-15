const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/app.js','utf8');
const html = fs.readFileSync('web/index.html','utf8');
const code = source.slice(source.indexOf('function renderSpeedMode('),source.indexOf('/* =================================================================== 设置 */'));
function setup(fail=false) {
  const elements = new Map(['#speed-mode','#cfg-speed-mode'].map(id=>[id,{value:'',disabled:false}]));
  const notices=[]; const posts=[];
  const state={config:{speed_mode:'balanced'}};
  const context=vm.createContext({S:state,$:id=>elements.get(id),toast:msg=>notices.push(msg),API:{post:async(url,body)=>{
    posts.push(body);
    assert.ok([...elements.values()].every(e=>e.disabled));
    if(fail)throw Error('offline');
    return {config:{speed_mode:body.patch.speed_mode}};
  }}});
  vm.runInContext(code,context);
  return {context,elements,notices,posts,state};
}
test('both selectors save and restore a persistent speed choice',async()=>{
  const h=setup();await h.context.switchSpeedMode('fast');
  assert.equal(h.posts[0].patch.speed_mode,'fast');
  assert.ok([...h.elements.values()].every(e=>e.value==='fast'&&!e.disabled));
  assert.equal(h.state.config.speed_mode,'fast');
});
test('save failure restores the previous mode and unlocks selectors',async()=>{
  const h=setup(true);await h.context.switchSpeedMode('deep');
  assert.ok([...h.elements.values()].every(e=>e.value==='balanced'&&!e.disabled));
  assert.match(h.notices[0],/保存失败/);
});
test('header and settings provide three clearly named speed options',()=>{
  for(const id of ['speed-mode','cfg-speed-mode']) {
    const section=html.match(new RegExp(`<select id="${id}"[^>]*>(.*?)</select>`,'s'))[1];
    for(const mode of ['fast','balanced','deep'])assert.ok(section.includes(`value="${mode}"`));
  }
});
