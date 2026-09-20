const $ = id => document.getElementById(id);
let matchesCache = {live: [], history: [], pending: [], stale: []};
let selectedGame = null;
let selectedKind = null;
let statusCache = {};
const phaseNames={idle:'暂无赛程',scheduled:'等待开赛',preparing:'比赛准备中',in_game:'进行中',paused:'比赛暂停',waiting_next_game:'等待下一局',finished:'系列赛已结束',finished_unverified:'待核验赛果'};

const pct = value => `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`;
const clock = seconds => `${Math.floor(seconds / 60)}:${String(Math.max(0, seconds % 60)).padStart(2, '0')}`;
const playedDate = item => item.frame.played_at ? new Intl.DateTimeFormat('zh-CN', {month:'long',day:'numeric'}).format(new Date(item.frame.played_at)) : '日期未知';
const modelName = kind => kind === 'experimental_baseline' ? '实验基线' : '逻辑回归';
const contest = item => item.frame.competition || {league_slug:'unknown',league_name:'未知赛区',tournament_name:'未知赛事',stage_name:'未知阶段'};

function option(value, text) {
  const item = document.createElement('option'); item.value = value; item.textContent = text; return item;
}

function setConnection(status) {
  statusCache = status;
  const node = $('connection');
  node.className = `connection ${status.provider_error ? 'error' : status.provider_enabled ? 'online' : 'offline'}`;
  node.replaceChildren(); node.append(Object.assign(document.createElement('span'), {}), status.provider_error ? '数据源异常' : status.provider_enabled ? (phaseNames[status.provider_phase]||'实时源已连接') : '历史数据模式');
  const trainedGames=status.model_training&&status.model_training.games;
  $('metric-model').textContent = `${modelName(status.model_kind)}${trainedGames?` · ${trainedGames}局`:''}`;
  const monitor=$('league-monitor'),leagues=status.leagues||[];monitor.replaceChildren();monitor.hidden=!leagues.length;
  leagues.forEach(item=>{const card=document.createElement('div'),name=document.createElement('strong'),state=document.createElement('span'),note=document.createElement('small');name.textContent=(item.competition&&item.competition.league_name)||String(item.league||'').toUpperCase();state.textContent=phaseNames[item.phase]||item.phase||'等待状态';note.textContent=item.error?'数据源异常':item.active_game_id?`Game ${item.active_game_id}`:item.scheduled_start?new Date(item.scheduled_start).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}):'等待赛程';card.className=item.error?'error':'';card.append(name,state,note);monitor.append(card);});
}

function fillLive(status) {
  const select = $('live-select'); select.replaceChildren(); const live = matchesCache.live;
  $('metric-live').textContent = live.length; $('live-count').textContent = `${live.length} 场`;
  if (!live.length) {
    select.append(option('', '暂无实时比赛')); select.disabled = true; $('open-live').disabled = true; $('live-empty').hidden = false;
    $('live-empty-copy').textContent = status.provider_enabled ? (status.provider_phase==='idle'?'当前配置的赛区没有进行中或待开始的比赛。':status.provider_phase==='preparing'?'已发现目标比赛，正在等待第一条字段完整的游戏内帧。':status.provider_phase==='scheduled'?'已发现后续赛程，将在开赛前一小时开始轮询比赛帧。':'实时数据源已连接，页面每 5 秒检查一次新比赛。') : '实时数据源尚未配置；历史比赛和曲线仍可正常查看。';
    return;
  }
  live.forEach(item => select.append(option(item.game_id, `${item.blue_name}  vs  ${item.red_name} · ${clock(item.game_time)}`)));
  select.disabled = false; $('open-live').disabled = false; $('live-empty').hidden = true;
  if (selectedKind === 'live' && live.some(x => String(x.game_id) === String(selectedGame))) select.value = selectedGame;
}

const archivedGames = () => matchesCache.history;

function fillArchiveFilters() {
  const history=archivedGames(),league=$('league-select'),tournament=$('tournament-select'),stage=$('stage-select');
  const reset=(node,label,values)=>{const previous=node.value;node.replaceChildren(option('',label));values.forEach(([value,text])=>node.append(option(value,text)));node.value=values.some(x=>x[0]===previous)?previous:'';};
  const leagueMap=new Map();history.forEach(item=>{const c=contest(item);leagueMap.set(c.league_slug,c.league_name);});
  reset(league,'全部赛区',[...leagueMap].sort((a,b)=>a[1].localeCompare(b[1],'zh-CN')));
  const inLeague=history.filter(item=>!league.value||contest(item).league_slug===league.value);
  reset(tournament,'全部赛事',[...new Set(inLeague.map(item=>contest(item).tournament_name))].sort().map(x=>[x,x]));
  const inTournament=inLeague.filter(item=>!tournament.value||contest(item).tournament_name===tournament.value);
  reset(stage,'全部阶段',[...new Set(inTournament.map(item=>contest(item).stage_name))].sort().map(x=>[x,x]));
}

function filteredHistory() {
  const league=$('league-select').value,tournament=$('tournament-select').value,stage=$('stage-select').value;
  return archivedGames().filter(item=>{const c=contest(item);return(!league||c.league_slug===league)&&(!tournament||c.tournament_name===tournament)&&(!stage||c.stage_name===stage);});
}

function fillHistory() {
  const history = [...filteredHistory()].sort((a,b) => new Date(b.frame.played_at || 0) - new Date(a.frame.played_at || 0));
  const total=archivedGames().length;$('metric-history').textContent = total; $('history-count').textContent = history.length===total?`${history.length} 局`:`${history.length} / ${total} 局`;
  const grouped = new Map();
  history.forEach(item => { const key=String(item.match_id); if(!grouped.has(key)) grouped.set(key,[]); grouped.get(key).push(item); });
  const series = $('series-select'), previous = series.value; series.replaceChildren();
  if (!history.length) { series.append(option('', '暂无历史数据')); series.disabled=true; fillHistoryGames(''); return; }
  series.disabled = false;
  grouped.forEach((items,id) => { const first=items[0],c=contest(first); series.append(option(id,`${playedDate(first)} · ${c.stage_name} · ${first.blue_name} vs ${first.red_name} · ${items.length} 局`)); });
  series.value = grouped.has(previous) ? previous : grouped.keys().next().value; fillHistoryGames(series.value);
}

function fillHistoryGames(matchId) {
  const select=$('history-select'); select.replaceChildren();
  const items=filteredHistory().filter(x=>String(x.match_id)===String(matchId)).sort((a,b)=>new Date(a.frame.played_at||0)-new Date(b.frame.played_at||0));
  if(!items.length){select.append(option('','请选择系列赛'));select.disabled=true;$('open-history').disabled=true;return;}
  items.forEach((item,index)=>{const winner=!item.finished?'待归档 / 赛果待核验':item.frame.winner_id==null?'赛果待核验':String(item.frame.winner_id)===String(item.frame.blue.id)?`${item.blue_name} 胜`:`${item.red_name} 胜`;select.append(option(item.game_id,`第 ${index+1} 局 · ${item.blue_name} vs ${item.red_name} · ${winner}`));});
  select.disabled=false;$('open-history').disabled=false;
  if(selectedKind==='history'&&items.some(x=>String(x.game_id)===String(selectedGame)))select.value=selectedGame;
}

function svgElement(name,attributes={}){const el=document.createElementNS('http://www.w3.org/2000/svg',name);Object.entries(attributes).forEach(([key,value])=>el.setAttribute(key,value));return el;}

function drawChart(points) {
  const svg=$('chart');svg.replaceChildren();const width=720,top=18,bottom=246,height=bottom-top;
  [0,.25,.5,.75,1].forEach(value=>svg.append(svgElement('line',{x1:0,y1:bottom-height*value,x2:width,y2:bottom-height*value,class:value===.5?'chart-grid center':'chart-grid'})));
  const times=$('chart-times');times.replaceChildren();if(!points.length)return;
  const maxTime=Math.max(1,points.at(-1).time),coords=points.map(point=>[point.time/maxTime*width,bottom-point.blue_probability*height]);
  const linePath=coords.map(([x,y],i)=>`${i?'L':'M'}${x.toFixed(1)},${y.toFixed(1)}`).join(' '),areaPath=`${linePath} L${coords.at(-1)[0].toFixed(1)},${bottom} L${coords[0][0].toFixed(1)},${bottom} Z`;
  const defs=svgElement('defs'),gradient=svgElement('linearGradient',{id:'area-gradient',x1:'0',y1:'0',x2:'0',y2:'1'});gradient.append(svgElement('stop',{offset:'0%','stop-color':'#55d8e5','stop-opacity':'.35'}),svgElement('stop',{offset:'100%','stop-color':'#55d8e5','stop-opacity':'0'}));defs.append(gradient);svg.append(defs);
  svg.append(svgElement('path',{d:areaPath,class:'chart-area'}),svgElement('path',{d:linePath,class:'chart-line'}));const [x,y]=coords.at(-1);svg.append(svgElement('circle',{cx:x,cy:y,r:10,class:'chart-dot-ring'}),svgElement('circle',{cx:x,cy:y,r:4,class:'chart-dot'}));
  [0,.25,.5,.75,1].forEach(ratio=>{const label=document.createElement('span');label.textContent=clock(Math.round(maxTime*ratio));times.append(label);});
}

function renderStats(frame) {
  const node=$('stats');node.replaceChildren();const specs=[['经济','gold',value=>value.toLocaleString()],['击杀','kills'],['防御塔','towers'],['小龙','drakes'],['Baron','nashors'],['兵营','inhibitors']];
  for(const [name,key,format=(x=>x)] of specs){const row=document.createElement('div');row.className='stat-row';const blue=document.createElement('strong');blue.textContent=format(frame.blue[key]);const label=document.createElement('span');label.textContent=name;const red=document.createElement('strong');red.textContent=format(frame.red[key]);const meter=document.createElement('i'),total=Math.max(1,frame.blue[key]+frame.red[key]);meter.style.setProperty('--blue-share',`${frame.blue[key]/total*100}%`);row.append(blue,label,red,meter);node.append(row);}
}

async function loadDetail(gameId,kind,scroll=true){
  if(!gameId)return;
  try{
    const response=await fetch(`/api/games/${encodeURIComponent(gameId)}`);if(!response.ok)throw new Error('详情暂时无法加载');const data=await response.json(),item=data.latest,frame=item.frame;selectedGame=String(gameId);selectedKind=kind;
    $('match-view').hidden=false;$('initial-empty').hidden=true;const finished=item.finished,hasWinner=frame.winner_id!=null,blueWon=hasWinner&&String(frame.winner_id)===String(frame.blue.id);
    const pending=!finished&&(matchesCache.pending||[]).some(x=>String(x.game_id)===String(item.game_id)),activePhase=pending?'finished_unverified':String(statusCache.active_game_id)===String(item.game_id)?statusCache.provider_phase:'in_game';
    const c=contest(item);$('detail-state').textContent=finished?(hasWinner?'MATCH ARCHIVE · 完整走势':'MATCH ARCHIVE · 赛果待核验'):pending?'MATCH PENDING · 等待赛果核验':'LIVE MATCH · 自动刷新';$('match-title').textContent=`${item.blue_name}  vs  ${item.red_name}`;$('match-meta').textContent=`${c.league_name} · ${c.tournament_name} · ${c.stage_name} · ${playedDate(item)} · 比赛 ID ${item.game_id}${finished&&hasWinner?` · ${blueWon?item.blue_name:item.red_name} 获胜`:''}`;
    $('state-badge').className=`state-badge ${finished?'finished':'live'}`;$('state-badge').textContent=finished?(hasWinner?`${blueWon?item.blue_name:item.red_name} 胜`:'赛果待核验'):`● ${phaseNames[activePhase]||'进行中'}`;
    $('blue-name').textContent=item.blue_name;$('red-name').textContent=item.red_name;$('stat-blue').textContent=item.blue_name;$('stat-red').textContent=item.red_name;
    $('blue-prob').textContent=pct(item.blue_probability);$('red-prob').textContent=pct(1-item.blue_probability);$('game-clock').textContent=clock(item.game_time);$('blue-bar').style.width=pct(item.blue_probability);$('bar-marker').style.left=pct(item.blue_probability);
    $('model-label').textContent=item.model_kind==='experimental_baseline'?'实验性基线 · 尚未校准':'已训练逻辑回归 · 尚未独立校准';
    const parts=[`服务端收到：${new Date(item.received_at*1000).toLocaleString()}`];if(frame.source_timestamp)parts.push(`数据帧：${new Date(frame.source_timestamp).toLocaleString()}`);if(Number.isFinite(Number(frame.lag_seconds)))parts.push(`供应商延迟：${Math.round(Number(frame.lag_seconds))} 秒`);if(frame.outcome_reconciled)parts.push('赛后核对胜方，曲线末点保留最后观测概率');$('updated-at').textContent=parts.join(' · ');
    const confirm=$('result-confirm');confirm.hidden=!(finished&&!hasWinner&&statusCache.manual_result_enabled);$('confirm-blue').textContent=`确认 ${item.blue_name} 胜`;$('confirm-red').textContent=`确认 ${item.red_name} 胜`;$('result-message').textContent='';
    drawChart(data.points);renderStats(frame);if(scroll)$('match-view').scrollIntoView({behavior:'smooth',block:'start'});
  }catch(error){$('connection').className='connection error';$('connection').replaceChildren(document.createElement('span'),error.message);}
}

async function refresh(){
  try{const [matchesResponse,statusResponse]=await Promise.all([fetch('/api/matches'),fetch('/api/status')]);if(!matchesResponse.ok||!statusResponse.ok)throw new Error();matchesCache=await matchesResponse.json();const status=await statusResponse.json();setConnection(status);fillLive(status);fillArchiveFilters();fillHistory();if(!selectedGame&&archivedGames().length){const first=$('history-select').value;if(first)await loadDetail(first,'history',false);}else if(selectedGame){const exists=[...matchesCache.live,...archivedGames()].some(x=>String(x.game_id)===selectedGame);if(exists)await loadDetail(selectedGame,selectedKind,false);}}catch(_){$('connection').className='connection error';$('connection').replaceChildren(document.createElement('span'),'连接中断');}
}

$('league-select').addEventListener('change',()=>{$('tournament-select').value='';$('stage-select').value='';fillArchiveFilters();fillHistory();});
$('tournament-select').addEventListener('change',()=>{$('stage-select').value='';fillArchiveFilters();fillHistory();});
$('stage-select').addEventListener('change',fillHistory);
$('series-select').addEventListener('change',event=>fillHistoryGames(event.target.value));
$('open-history').addEventListener('click',()=>loadDetail($('history-select').value,'history'));
$('history-select').addEventListener('change',()=>loadDetail($('history-select').value,'history',false));
$('open-live').addEventListener('click',()=>loadDetail($('live-select').value,'live'));
$('live-select').addEventListener('change',()=>loadDetail($('live-select').value,'live',false));
async function confirmResult(side){const source=$('result-source').value.trim(),message=$('result-message');if(!source.startsWith('https://')){message.textContent='请填写 https:// 开头的回放或逐局赛果链接。';return;}const buttons=[$('confirm-blue'),$('confirm-red')];buttons.forEach(x=>x.disabled=true);message.textContent='正在保存核验结果…';try{const response=await fetch(`/api/games/${encodeURIComponent(selectedGame)}/finalize`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({winner_side:side,label_source:source})});const data=await response.json();if(!response.ok)throw new Error(data.error||'保存失败');message.textContent='赛果已保存；该局会进入下一次批量训练。';await refresh();}catch(error){message.textContent=error.message;}finally{buttons.forEach(x=>x.disabled=false);}}
$('confirm-blue').addEventListener('click',()=>confirmResult('blue'));
$('confirm-red').addEventListener('click',()=>confirmResult('red'));
refresh();setInterval(refresh,5000);
