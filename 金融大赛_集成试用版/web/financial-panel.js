/* Reuse the teammate UI and mount the local extraction workspace in one origin. */
(() => {
  const dialog=document.createElement('dialog');
  dialog.id='finance-dialog';
  dialog.setAttribute('aria-label','资料与来源复核');
  const bar=document.createElement('div');bar.className='finance-bar';
  const title=document.createElement('strong');title.textContent='资料与来源复核';
  const close=document.createElement('button');close.textContent='返回对话';
  close.onclick=()=>dialog.close();
  const frame=document.createElement('iframe');frame.title='金融提取和证据复核工作台';
  frame.src='/finance/';
  bar.append(title,close);dialog.append(bar,frame);document.body.append(dialog);
  function open(){dialog.showModal();}
  const button=document.createElement('button');button.className='btn-side';
  button.textContent='资料与来源复核';button.onclick=open;
  document.querySelector('.side-foot').prepend(button);
  for(const [label,url] of [['可选估值方案模板','/api/research-templates/valuation-assumptions.json'],['下载财务数据模板','/api/research-templates/financial-data.csv']]){
    const a=document.createElement('a');a.className='btn-side';a.textContent=label;a.href=url;a.setAttribute('download','');document.querySelector('.side-foot').append(a);
  }
})();
