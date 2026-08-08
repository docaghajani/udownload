const defaults={intercept:true,sendCookies:true};
const status=document.getElementById('status');
const interceptBox=document.getElementById('intercept');
const cookiesBox=document.getElementById('sendCookies');
chrome.storage.local.get(defaults,s=>{interceptBox.checked=s.intercept;cookiesBox.checked=s.sendCookies});
interceptBox.addEventListener('change',e=>chrome.storage.local.set({intercept:e.target.checked}));
cookiesBox.addEventListener('change',e=>chrome.storage.local.set({sendCookies:e.target.checked}));
async function act(action,mode){status.textContent='Working…';const r=await chrome.runtime.sendMessage({action,mode});status.textContent=r?.ok===false?r.error:'Sent to UDM';}
document.getElementById('all').onclick=()=>act('collect','all');
document.getElementById('selected').onclick=()=>act('collect','selected');
document.getElementById('test').onclick=()=>act('ping');
