const API="/api/ext/funhelper";
const $=s=>document.querySelector(s);
const $$=s=>document.querySelectorAll(s);
const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let toastT;
function toast(m,t){const el=document.getElementById("toast");if(el){el.textContent=m;el.hidden=false;el.className="toast show "+(t||"");clearTimeout(toastT);toastT=setTimeout(()=>el.classList.remove("show"),2400);}}
function errToast(e){toast(e&&e.message||"请求失败","error");}
let CURRENT_GID="";
async function api(method,path,body){const opt={method,headers:{"Content-Type":"application/json"}};if(body)opt.body=JSON.stringify(body);const sep=path.indexOf("?")>=0?"&":"?";const url=API+"/"+path+(CURRENT_GID?sep+"gid="+encodeURIComponent(CURRENT_GID):"");const res=await fetch(url,opt);const d=await res.json().catch(()=>({}));if(!res.ok||d.success!==true)throw new Error((d.error&&d.error.message)||d.error||"请求失败");return d;}
function shortId(id){const s=String(id||"");return s.length>12?s.slice(0,6)+"…"+s.slice(-4):s;}
function disp(u){let n=String(u.nickname||"").replace(/[\u3164\u200b\u200c\u200d\s\u00a0]+/g,"").trim();if(n&&n!==String(u.id))return n;return "";}
function dispId(u){const q=(u.qq||"").trim();if(q&&q.isdigit?q.isdigit():/^\d+$/.test(q))return q;return shortId(u.id);}
function avatarHtml(u){if(u&&u.avatar)return '<img class="avatar-img" src="'+esc(u.avatar)+'" alt="" onerror="this.style.display=\'none\'">';const nm=(u.nickname||"").trim()||u.id;const l=String(nm).slice(-2).toUpperCase()||"?";return '<span class="avatar">'+esc(l)+"</span>";}
const COMMANDS=[
{n:"娱乐菜单",d:"查看全部指令",tag:"系统"},
{n:"签到",d:"每日1次，随机得积分",tag:"积分"},{n:"抽奖",d:"花积分抽奖，带中奖率",tag:"积分"},
{n:"我的",d:"查看积分/反甲/排名/签到",tag:"积分"},{n:"积分排行",d:"查看全群排行榜",tag:"积分"},
{n:"抢劫 @某人",d:"随机抢积分，失败反扣",tag:"互动"},{n:"同归于尽 @某人",d:"双方各扣积分少者的全部",tag:"互动"},
{n:"购买反甲 [数量]",d:"花积分购护盾防抢，如 购买反甲2",tag:"互动"},{n:"单身狗 @某人 或 QQ号",d:"生成单身狗恶搞配图",tag:"互动"},{n:"马内 @某人 或 QQ号",d:"生成求财配图",tag:"互动"},{n:"发红包 积分 份数 口令",d:"口令1~4位数字，30分钟未领完退回",tag:"红包"},
{n:"抢红包 [口令]",d:"直接抢或按口令抢",tag:"红包"},{n:"红包列表",d:"查看可抢红包",tag:"红包"},
{n:"禁言 @某人 [分钟]",d:"花积分禁言，默认1分钟",tag:"消耗"},{n:"撤回",d:"引用消息后发送本指令撤回",tag:"消耗"},
{n:"生图 描述",d:"花积分AI绘图，弹按钮选比例",tag:"消耗"},{n:"添加积分 @",d:"管理员加积分",tag:"管理"},{n:"删除积分 @",d:"管理员扣积分",tag:"管理"},
{n:"台风",d:"最强/活跃台风查询出图",tag:"系统"},{n:"域名信息",d:"Whois 域名查询，如 域名 baidu.com",tag:"系统"},{n:"图床",d:"发「图床」后发图/视频自动上传",tag:"系统"},{n:"插画",d:"随机二次元插画，图库每小时更新",tag:"系统"},
];
const CAT_ORDER=["积分","互动","红包","消耗","管理","系统"];
const TITLES={overview:"数据总览",config:"规则配置",users:"用户管理",redpacks:"红包管理",about:"指令说明"};
function switchPage(name){const btns=document.querySelectorAll(".bottom-nav button");btns.forEach(b=>b.classList.toggle("active",b.dataset.page===name));document.querySelectorAll(".page").forEach(p=>p.classList.toggle("active",p.id==="page-"+name));const t=document.getElementById("page-title");if(t)t.textContent=TITLES[name]||"";window.scrollTo(0,0);}
function bindNav(){document.querySelectorAll(".bottom-nav button[data-page]").forEach(b=>{b.onclick=()=>switchPage(b.dataset.page);});document.querySelectorAll("[data-open-page]").forEach(b=>{b.onclick=()=>switchPage(b.dataset.openPage);});}
document.addEventListener("DOMContentLoaded",()=>{init();});
let ALL_USERS=[],CONFIG={},PACK_COUNT=0;
async function loadUsers(){try{ALL_USERS=(await api("GET","users")).data||[];renderUsers();renderTop();}catch(e){errToast(e);}}
async function loadConfig(){try{CONFIG=(await api("GET","config")).data||{};renderConfigForm();renderRulesSummary();}catch(e){errToast(e);}}
async function loadPacks(){try{const r=(await api("GET","redpacks")).data||[];PACK_COUNT=r.length;renderPacks(r);}catch(e){errToast(e);}}
let groupsLoaded=false;
async function loadGroups(){try{const list=(await api("GET","groups")).data||[];const sel=document.getElementById("gid-select");if(!sel)return;sel.innerHTML="";const add=(id,label,sub)=>{const o=document.createElement("option");o.value=id;o.textContent=label+(sub?" · "+sub:"");sel.appendChild(o);};if(!list.length){add("","(暂无群，请先在群里发任意娱乐命令)");}else{list.forEach(g=>{const id=g.id;const nm=g.name||shortId(id);const sub=g.legacy?"[历史]":(g.members>0?`${g.members}人`:"");add(id,nm,sub);});}if(!CURRENT_GID&&list.length){CURRENT_GID=list[0].id;loadUsers();loadPacks();}sel.value=CURRENT_GID||"";if(sel.onchange)return;sel.onchange=()=>{CURRENT_GID=sel.value;loadUsers();loadConfig();loadPacks();const _sel=document.getElementById("gid-select");const _nm=_sel&&_sel.selectedOptions[0]?_sel.selectedOptions[0].textContent:CURRENT_GID;toast(CURRENT_GID?("已切换到群 "+_nm):"未选择群","ok");};groupsLoaded=true;}catch(e){errToast(e);}}
function bindSidebar(){const at=document.getElementById("sidebar-toggle");if(at)at.onclick=()=>document.getElementById("app").classList.toggle("sidebar-open");const sc=document.getElementById("sidebar-scrim");if(sc)sc.onclick=()=>document.getElementById("app").classList.remove("sidebar-open");}
function bindSearch(){const s=document.getElementById("user-search");if(!s)return;s.addEventListener("input",()=>{const k=s.value.trim().toLowerCase();renderUsers(ALL_USERS.filter(u=>!k||disp(u).toLowerCase().includes(k)||String(u.id).includes(k)));});}
function renderTop(){const el=document.getElementById("top-list");if(!el||!ALL_USERS)return;const ext=u=>u.armor>0;const top=ALL_USERS.slice(0,8);el.innerHTML=top.length?top.map((u,i)=>'<div class="row"><span class="u'+(ext(u)?" has-armor":"")+'"><span class="rank'+(i<3?" g"+(i+1):"")+'">'+(i+1)+'</span>'+avatarHtml(u)+'<div>'+esc(disp(u))+'</div></span><span class="points-val">'+u.points+'</span></div>').join(""):'<div class="empty">暂无用户</div>';const mf=(id,v)=>{const m=document.getElementById(id);if(m)m.textContent=v;};mf("m-users",ALL_USERS.length);mf("m-points",ALL_USERS.reduce((a,u)=>a+u.points,0));mf("m-armor",ALL_USERS.reduce((a,u)=>a+u.armor,0));mf("m-packs",PACK_COUNT);}
function renderRulesSummary(){const el=document.getElementById("rules-summary");if(!el)return;const R=[["签到",(CONFIG.sign_lo||50)+" ~ "+(CONFIG.sign_hi||200)+" 分"],["抽奖","成本 "+(CONFIG.lottery_cost||20)+" · 命中 "+(Math.round((CONFIG.lottery_win_rate||0)*100))+"% · 得 "+(CONFIG.lottery_lo||0)+"~"+(CONFIG.lottery_hi||150)+" 分"],["抢劫","成功 "+(Math.round((CONFIG.robbery_rate||0)*100))+"% · 得 "+(CONFIG.robbery_lo||0)+"~"+(CONFIG.robbery_hi||150)+" 分"],["反甲","购买 "+(CONFIG.armor_cost||100)+" 分"]].map(x=>'<div class="row"><span>'+x[0]+'</span><b>'+x[1]+'</b></div>').join("");el.innerHTML=R;}
function renderChahuaStats(){const d=window.CHAHUA_STATS||{};const el=document.getElementById("chahua-stats");if(!el)return;const fmt=v=>{if(!v)return "从未";const t=new Date(v*1000);return (t.getMonth()+1)+"-"+t.getDate()+" "+String(t.getHours()).padStart(2,"0")+":"+String(t.getMinutes()).padStart(2,"0");};const nxt=d.next_refresh_in?Math.ceil(d.next_refresh_in/60)+" 分钟后":(d.refresh_running?"采集中":"-");const rows=[
["图库数量",(d.cards||0)+" 张","自动更新每 "+(d.interval_min||60)+" 分钟"],
["上次采集",fmt(d.last_refresh),d.refresh_running?"⏳ 采集中…":(d.refresh_result||"")],
["下次采集",nxt,"后台定时任务"],
["AI 检测",(d.detect_total||0)+" 次","拦截 "+(d.detect_blocked||0)+" 张"],
["QQ 违规",(d.violations||0)+" 次","已进黑名单 "+(d.blacklist||0)+" 条"],
["检测模型",d.model_ready?(d.model_loaded?"已就绪":"已就绪 · 首次发图时加载"):"未安装(自动放行)","本地 nudenet"],
];el.innerHTML=rows.map(r=>'<div class="row"><span>'+esc(r[0])+'</span><b>'+esc(r[1])+' <small>'+esc(r[2])+'</small></b></div>').join("");}
async function loadChahuaStats(){try{window.CHAHUA_STATS=(await api("GET","chahua_stats")).data||{};renderChahuaStats();}catch(e){/* 静默 */}}
const CFG_MAP={sign_lo:"cfg-sign-lo",sign_hi:"cfg-sign-hi",lottery_cost:"cfg-lottery-cost",lottery_lo:"cfg-lottery-lo",lottery_hi:"cfg-lottery-hi",lottery_win_rate:"cfg-lottery-win",robbery_lo:"cfg-robbery-lo",robbery_hi:"cfg-robbery-hi",robbery_rate:"cfg-robbery-rate",mute_cost:"cfg-mute-cost",revoke_cost:"cfg-revoke-cost",draw_cost:"cfg-draw-cost",armor_cost:"cfg-armor-cost",draw_api_base:"cfg-draw-api-base",draw_api_key:"cfg-draw-api-key",draw_model:"cfg-draw-model",draw_proxy:"cfg-draw-proxy"};
const CFG_TEXT=new Set(["draw_api_base","draw_api_key","draw_model","draw_proxy"]);
function renderConfigForm(){for(const k in CFG_MAP){const e=document.getElementById(CFG_MAP[k]);if(!e)continue;let v=CONFIG[k];if(CFG_TEXT.has(k))v=v||"";else if(k.indexOf("rate")>=0)v=Math.round((v||0)*100);else v=v||0;e.value=v;}}
function renderCommands(){const el=document.getElementById("cmd-grid");if(!el)return;el.innerHTML=COMMANDS.map(c=>'<div class="cmd-item"><b>'+esc(c.n)+'</b><small>'+esc(c.d)+'</small><span class="cmd-tag">'+esc(c.tag)+'</span></div>').join("");}
function renderUsers(list){const rows=list||ALL_USERS;const tb=document.getElementById("users-tbody");if(!tb)return;tb.innerHTML=rows.length?rows.map((u,i)=>'<tr data-id="'+esc(u.id)+'"><td><span class="rank">'+(i+1)+'</span></td><td><div class="user-cell">'+avatarHtml(u)+'<div><b>'+esc(disp(u))+'</b><small class="row-id">ID: '+esc(dispId(u))+'</small></div></div></td><td class="pcell">'+u.points+'</td><td>'+u.armor+'</td><td><div class="adjust"><input class="adj-val" type="number" placeholder="数量"><button class="adj-btn add">＋加分</button><button class="adj-btn sub">－扣分</button><button class="adj-btn del">删除</button></div></td></tr>').join(""):'<tr><td colspan="5" class="empty">暂无用户</td></tr>';
tb.querySelectorAll("tr[data-id]").forEach(tr=>{const id=tr.dataset.id;const inp=tr.querySelector(".adj-val");const change=async(dir)=>{const n=parseInt(inp&&inp.value,10);if(!n){toast("请输入数量","error");return;}const amt=Math.abs(n);try{await api("POST",dir>0?"points_add":"points_sub",{user_id:id,amount:amt});toast((dir>0?"已加分 ":"已扣分 ")+amt,"ok");if(inp)inp.value="";loadUsers();}catch(e){toast(e.message,"error");}};const badd=tr.querySelector(".adj-btn.add");if(badd)badd.onclick=()=>change(1);const bsub=tr.querySelector(".adj-btn.sub");if(bsub)bsub.onclick=()=>change(-1);const bdel=tr.querySelector(".adj-btn.del");if(bdel)bdel.onclick=async()=>{const u=ALL_USERS.find(x=>x.id===id);const nm=u?disp(u):shortId(id);if(!confirm("确定删除用户 "+nm+" 的全部记录？"))return;try{const r=await api("POST","delete",{user_id:id});if(r.data&&r.data.removed){toast("已删除用户","ok");loadUsers();}else{toast("未找到该用户","error");}}catch(e){toast(e.message,"error");}};});}
function renderPacks(list){const el=document.getElementById("redpack-list");if(!el)return;el.innerHTML=list.length?list.map(p=>'<div class="redpack-card"><div class="redpack-ico">🧧</div><div class="redpack-info"><b>红包 #'+esc(p.id)+'</b><small>由 '+esc(p.sender_name||shortId(p.sender))+' · 每份 '+esc(p.amount)+' 分 · 剩 '+esc(p.left)+'</small></div></div>').join(""):'<div class="empty">暂无红包</div>';}
function saveConfig(){const cfg={};for(const k in CFG_MAP){const e=document.getElementById(CFG_MAP[k]);if(!e)continue;if(CFG_TEXT.has(k)){cfg[k]=e.value.trim();continue;}let v=parseFloat(e.value);if(k.indexOf("rate")>=0)v=v/100;else if(v||v===0)v=Math.round(v);cfg[k]=v;}const btn=document.getElementById("save-config");if(btn)btn.disabled=true;api("POST","config",cfg).then(()=>{toast("配置已保存","ok");loadConfig();}).catch(e=>errToast(e)).finally(()=>{if(btn)btn.disabled=false;});}
function init(){bindNav();bindSidebar();bindSearch();renderCommands();switchPage("overview");loadGroups();loadUsers();loadConfig();loadPacks();loadChahuaStats();const sc=document.getElementById("save-config");if(sc)sc.onclick=saveConfig;const rb=document.getElementById("refresh-btn");if(rb)rb.onclick=()=>{loadUsers();loadConfig();loadPacks();loadChahuaStats();toast("已刷新","ok");};bindCfgTabs();}


// ==================== 配置页悬浮导航 (规则/生图) ====================
async function saveDrawConfig(){
  // 只提交生图相关字段 (draw_*), 不动规则配置
  const cfg = {};
  for(const k in CFG_MAP){
    if(!k.startsWith('draw_')) continue;
    const e = document.getElementById(CFG_MAP[k]);
    if(!e) continue;
    if(CFG_TEXT.has(k)){ cfg[k] = e.value.trim(); continue; }
    let v = parseFloat(e.value);
    if(k.indexOf('rate') >= 0) v = v/100;
    else if(v || v === 0) v = Math.round(v);
    cfg[k] = v;
  }
  try{
    const r = await api('POST', 'config', cfg);
    toast('生图配置已保存', 'ok');
  }catch(e){ errToast(e); }
}

function bindCfgTabs(){
  const nav = document.querySelector('.cfg-nav');
  if(!nav) return;
  nav.querySelectorAll('button').forEach(btn => {
    btn.onclick = () => {
      nav.querySelectorAll('button').forEach(b => b.classList.toggle('active', b === btn));
      const tab = btn.dataset.cfgTab;
      const rulesEl = document.getElementById('cfg-rules');
      const hostingEl = document.getElementById('cfg-hosting');
      if(rulesEl){ rulesEl.hidden = (tab !== 'rules'); rulesEl.classList.toggle('tab-active', tab === 'rules'); }
      if(hostingEl){ hostingEl.hidden = (tab !== 'hosting'); hostingEl.classList.toggle('tab-active', tab === 'hosting'); }
    };
  });
  const sd = document.getElementById('save-draw-config');
  if(sd) sd.onclick = saveDrawConfig;
}
