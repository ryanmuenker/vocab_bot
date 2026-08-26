export const INSPECTOR_HTML = String.raw`<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hermes Memory Atlas</title>
<style>
*{box-sizing:border-box}html,body{width:100%;height:100%;margin:0;overflow:hidden}button,input,select{font:inherit}button{color:inherit}button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
:root{--ink:#25251f;--paper:#d7d0a8;--panel:#eee8d2;--rail:#d0c79b;--muted:#746f59;--new:#b58834;--learning:#a24634;--memorized:#315f4e;--focus:#1c69a8}
body{background:var(--paper);color:var(--ink);font-family:"Courier New",ui-monospace,monospace;font-size:12px}
[hidden]{display:none!important}.app{width:100vw;height:100vh;display:grid;grid-template-rows:44px minmax(0,1fr) 24px;border:1px solid var(--ink)}
header{display:grid;grid-template-columns:minmax(122px,1.5fr) repeat(4,minmax(54px,.8fr)) minmax(126px,1.25fr);border-bottom:1px solid var(--ink)}
.name,.count,.controls{display:flex;align-items:center;padding:0 clamp(6px,1vw,12px);border-right:1px solid color-mix(in srgb,var(--ink) 34%,transparent);min-width:0}.name{font-weight:700;font-size:clamp(9px,1.4vw,13px);letter-spacing:.04em;white-space:nowrap}.count{justify-content:space-between;gap:4px;font-size:clamp(6px,.8vw,9px);text-transform:uppercase;color:var(--muted)}.count b{font-size:clamp(11px,1.5vw,16px);color:var(--ink)}.controls{gap:5px;border:0}.control,.mode-button,.zoom-button{border:1px solid var(--ink);background:transparent;padding:7px 8px;font-weight:700;font-size:8px;text-transform:uppercase;cursor:pointer}.control:hover,.mode-button:hover,.zoom-button:hover,.filter:hover,.word-node:hover{background:color-mix(in srgb,var(--panel) 48%,transparent)}
.main{display:grid;grid-template-columns:clamp(102px,13vw,154px) minmax(250px,1fr) clamp(220px,28vw,340px);min-height:0}.filters{border-right:1px solid var(--ink);background:var(--rail);padding:9px;overflow:auto}.search{width:100%;padding:8px;border:1px solid var(--ink);background:color-mix(in srgb,var(--panel) 58%,transparent);font-size:10px;margin-bottom:7px;color:var(--ink)}.filter{width:100%;display:grid;grid-template-columns:8px 1fr auto;gap:6px;align-items:center;padding:9px 2px;border:0;border-top:1px solid color-mix(in srgb,var(--ink) 36%,transparent);background:transparent;font-size:8px;text-align:left;text-transform:uppercase;letter-spacing:.05em;cursor:pointer}.filter i{width:6px;height:6px;background:var(--tone);border:1px solid var(--ink)}.filter[aria-pressed="true"]{font-weight:700;background:color-mix(in srgb,var(--panel) 28%,transparent)}.sort-label{display:block;margin-top:17px;padding-top:8px;border-top:1px solid var(--ink);font-size:7px;text-transform:uppercase;color:var(--muted)}.sort{width:100%;margin-top:6px;padding:7px;border:1px solid var(--ink);background:transparent;color:var(--ink);font-size:8px}.status-message{min-height:28px;margin-top:12px;font-size:8px;line-height:1.4;color:var(--learning)}
.workspace{position:relative;min-width:0;min-height:0;overflow:hidden;background-color:var(--paper);background-image:linear-gradient(30deg,#25251f12 12%,transparent 12.5%,transparent 87%,#25251f12 87.5%),linear-gradient(150deg,#25251f12 12%,transparent 12.5%,transparent 87%,#25251f12 87.5%),linear-gradient(30deg,#25251f12 12%,transparent 12.5%,transparent 87%,#25251f12 87.5%);background-size:36px 63px;background-position:0 0,0 0,18px 31.5px}.atlas{position:absolute;inset:0;overflow:hidden;touch-action:none;cursor:grab}.atlas.dragging{cursor:grabbing}.atlas-world{position:absolute;left:0;top:0;width:100%;min-height:100%;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));transform-origin:0 0;will-change:transform}.lane{min-height:100%;padding:30px clamp(5px,1vw,10px) 42px;border-right:1px dashed color-mix(in srgb,var(--ink) 34%,transparent)}.lane:last-child{border-right:0}.lane-title{position:sticky;top:0;z-index:2;display:flex;justify-content:space-between;margin:-20px 0 11px;font-size:7px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}.lane-nodes{display:grid;grid-template-columns:repeat(2,minmax(86px,1fr));gap:18px 14px;align-items:start}.word-node{--tone:var(--new);--lift:2px;position:relative;min-width:0;padding:7px 8px 8px;border:1px solid var(--ink);background:var(--panel);box-shadow:var(--lift) var(--lift) 0 var(--ink);text-align:left;cursor:pointer;transition:transform .12s ease,box-shadow .12s ease}.word-node:before{content:"";position:absolute;left:-1px;right:-1px;top:-4px;height:3px;background:var(--tone);border:1px solid var(--ink)}.word-node:hover,.word-node[aria-current="true"]{transform:translate(-2px,-2px);box-shadow:7px 7px 0 var(--ink);z-index:1}.word-node strong{display:block;overflow:hidden;text-overflow:ellipsis;font-size:9px;white-space:nowrap}.word-node small{display:block;margin-top:4px;font-size:7px;color:var(--muted)}.word-node.due:after{content:"";position:absolute;width:5px;height:5px;border-radius:50%;right:5px;top:5px;background:var(--learning);box-shadow:0 0 0 3px color-mix(in srgb,var(--learning) 22%,transparent);animation:pulse 1.8s ease-in-out infinite}.word-node[data-status="learning"]{--tone:var(--learning)}.word-node[data-status="memorized"]{--tone:var(--memorized)}@keyframes pulse{50%{box-shadow:0 0 0 6px transparent}}
.mode-switch{position:absolute;left:9px;bottom:9px;z-index:4;display:flex;background:var(--panel)}.mode-button[aria-pressed="true"]{background:var(--ink);color:var(--panel)}.zoom-controls{position:absolute;right:9px;bottom:9px;z-index:4;display:flex;gap:4px}.zoom-button{width:27px;height:27px;padding:0;font-size:14px;background:var(--panel)}
.table-view{position:absolute;inset:0;overflow:auto;background:var(--panel);padding-bottom:42px}.word-table{width:100%;border-collapse:collapse;font-size:9px}.word-table th{position:sticky;top:0;z-index:2;background:var(--ink);color:var(--panel);padding:8px;text-align:left;text-transform:uppercase;font-size:7px;letter-spacing:.07em}.word-table td{padding:8px;border-bottom:1px solid color-mix(in srgb,var(--ink) 28%,transparent);vertical-align:top}.word-table tr[data-selected="true"]{background:color-mix(in srgb,var(--memorized) 12%,var(--panel))}.entry-button{border:0;background:transparent;padding:0;text-align:left;font-weight:700;cursor:pointer}.definition-cell{max-width:250px;color:var(--muted)}
.detail{border-left:1px solid var(--ink);background:var(--panel);padding:clamp(9px,1.2vw,15px);overflow:auto}.empty-detail{display:grid;place-items:center;height:100%;color:var(--muted);font-size:9px}.titleline{display:flex;align-items:flex-start;justify-content:space-between;gap:7px;border-bottom:1px solid var(--ink);padding-bottom:9px}.entry-title{margin:0;font-size:clamp(17px,2vw,25px);font-weight:500;line-height:1;letter-spacing:-.04em;overflow-wrap:anywhere}.tag{border:1px solid var(--tone);color:var(--tone);padding:4px 5px;font-size:7px;text-transform:uppercase;white-space:nowrap}.meta{font-size:7px;color:var(--muted);margin-top:6px}.sense{padding:10px 0;border-bottom:1px solid color-mix(in srgb,var(--ink) 34%,transparent)}.sense b{display:block;font-size:7px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}.sense p{margin:6px 0 0;font-size:11px;line-height:1.45}.example{margin-top:7px;padding-left:8px;border-left:2px solid var(--memorized);font-size:8px;line-height:1.45;color:var(--muted)}.memory{width:100%;margin-top:12px;border-collapse:collapse;font-size:8px}.memory th{background:var(--ink);color:var(--panel);font-size:7px;text-align:left;text-transform:uppercase}.memory th,.memory td{padding:7px;border:1px solid var(--ink)}.recent{margin-top:13px}.recent h3{margin:0 0 5px;font-size:7px;text-transform:uppercase;letter-spacing:.08em}.attempt{display:grid;grid-template-columns:1fr 44px 42px;gap:4px;padding:6px 0;border-top:1px solid color-mix(in srgb,var(--ink) 28%,transparent);font-size:7px;color:var(--muted)}.attempt b{color:var(--ink)}
.danger-zone{margin-top:18px;padding-top:12px;border-top:1px solid var(--learning)}.danger-zone h3{margin:0 0 6px;font-size:8px;text-transform:uppercase;letter-spacing:.08em;color:var(--learning)}.danger-copy{margin:0 0 9px;color:var(--muted);font-size:8px;line-height:1.45}.delete-entry{width:100%;border:1px solid var(--learning);background:transparent;color:var(--learning);padding:8px;font-size:8px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;cursor:pointer}.delete-entry:hover{background:var(--learning);color:var(--panel)}.delete-entry:disabled{cursor:wait;opacity:.58}.danger-status{min-height:14px;margin-top:6px;color:var(--learning);font-size:7px}
footer{display:flex;align-items:center;justify-content:space-between;padding:0 8px;border-top:1px solid var(--ink);font-size:7px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}.legend{display:flex;gap:12px}.legend span:before{content:"";display:inline-block;width:5px;height:5px;margin-right:4px;background:var(--tone);border:1px solid var(--ink)}
.gate{position:fixed;inset:0;z-index:20;display:grid;place-items:center;background:var(--paper)}.gate-panel{width:min(340px,calc(100vw - 32px));border:1px solid var(--ink);background:var(--panel);padding:22px;box-shadow:8px 8px 0 var(--ink)}.gate-panel h1{margin:0 0 16px;font-size:18px;letter-spacing:.03em}.gate-form{display:grid;grid-template-columns:1fr auto}.token-input{min-width:0;border:1px solid var(--ink);border-right:0;background:transparent;padding:9px;color:var(--ink)}.connect{border:1px solid var(--ink);background:var(--ink);color:var(--panel);padding:9px 12px;font-weight:700;text-transform:uppercase;cursor:pointer}.gate-error{min-height:16px;margin-top:9px;color:var(--learning);font-size:8px}
@media(max-width:760px){header{grid-template-columns:minmax(95px,1fr) repeat(4,44px) 96px}.count{flex-direction:column;justify-content:center;gap:0;padding:0 2px}.count span{display:block;font-size:5px}.controls{padding:0 3px}.controls .control{padding:6px 3px;font-size:6px}.main{grid-template-columns:92px minmax(210px,1fr) 210px}.detail{padding:8px}.lane-nodes{grid-template-columns:1fr}}
@media(max-width:520px){.app{grid-template-rows:40px minmax(0,1fr) 22px}header{grid-template-columns:minmax(96px,1fr) repeat(4,32px) 64px}.name{font-size:8px;padding:0 5px}.count{padding:0 1px}.count span{font-size:4px}.count b{font-size:9px}.controls{gap:2px;padding:0 2px}.controls .control{min-width:0;padding:5px 1px;font-size:5px;overflow:hidden}.main{grid-template-columns:72px minmax(0,1fr);grid-template-rows:minmax(220px,55%) minmax(0,45%)}.filters{grid-row:1/3;padding:5px}.search{padding:6px 4px;font-size:7px}.filter{grid-template-columns:6px 1fr auto;gap:3px;padding:7px 1px;font-size:6px}.filter i{width:5px;height:5px}.sort-label{margin-top:10px}.sort{padding:5px 2px;font-size:6px}.workspace{grid-column:2;grid-row:1}.detail{grid-column:2;grid-row:2;border-top:1px solid var(--ink);border-left:1px solid var(--ink)}.lane{padding:26px 3px 38px}.lane-title{margin:-18px 0 8px;font-size:5px}.word-node{padding:6px 4px}.word-node strong{font-size:6px}.word-node small{font-size:5px}.mode-switch{left:5px;bottom:5px}.mode-button{padding:5px;font-size:6px}.zoom-controls{right:5px;bottom:5px}.zoom-button{width:23px;height:23px}.entry-title{font-size:16px}.sense p{font-size:9px}.legend{gap:4px}#generated-at{display:none}}
@media(prefers-reduced-motion:reduce){.word-node{transition:none}.word-node.due:after{animation:none}}
</style>
</head>
<body data-inspector>
<div id="gate" class="gate">
  <div class="gate-panel">
    <h1>Memory Atlas</h1>
    <form id="token-form" class="gate-form">
      <input id="token" class="token-input" type="password" autocomplete="current-password" placeholder="Admin token" aria-label="Admin token" required>
      <button class="connect" type="submit">Open</button>
    </form>
    <div id="gate-error" class="gate-error" role="alert"></div>
  </div>
</div>
<div id="app" class="app" hidden>
<header>
  <div class="name">MEMORY ATLAS</div>
  <div class="count"><span>Words</span><b id="count-total">0</b></div>
  <div class="count"><span>Unseen</span><b id="count-unseen">0</b></div>
  <div class="count"><span>Learning</span><b id="count-learning">0</b></div>
  <div class="count"><span>Memorized</span><b id="count-memorized">0</b></div>
  <div class="controls"><button id="refresh" class="control" type="button">Refresh</button><button id="mode-table-top" class="control" type="button">Table</button></div>
</header>
<div class="main">
  <nav class="filters" aria-label="Vocabulary filters">
    <input id="search" class="search" type="search" placeholder="Search…" aria-label="Search words and definitions">
    <button class="filter" type="button" data-filter="all" aria-pressed="true" style="--tone:var(--ink)"><i></i><span>All</span><b id="filter-all">0</b></button>
    <button class="filter" type="button" data-filter="unseen" aria-pressed="false" style="--tone:var(--new)"><i></i><span>Unseen</span><b id="filter-unseen">0</b></button>
    <button class="filter" type="button" data-filter="learning" aria-pressed="false" style="--tone:var(--learning)"><i></i><span>Learning</span><b id="filter-learning">0</b></button>
    <button class="filter" type="button" data-filter="memorized" aria-pressed="false" style="--tone:var(--memorized)"><i></i><span>Memorized</span><b id="filter-memorized">0</b></button>
    <button class="filter" type="button" data-filter="due" aria-pressed="false" style="--tone:var(--learning)"><i></i><span>Due</span><b id="filter-due">0</b></button>
    <label class="sort-label" for="sort">Sort</label>
    <select id="sort" class="sort"><option value="stability">Stability</option><option value="added">Added</option><option value="due">Due date</option><option value="alphabetical">Alphabetical</option></select>
    <div id="status-message" class="status-message" role="status"></div>
  </nav>
  <section class="workspace" aria-label="Vocabulary workspace">
    <div id="atlas-view" class="atlas">
      <div id="atlas-world" class="atlas-world">
        <section class="lane"><div class="lane-title"><span>Unseen</span><b id="lane-unseen-count">0</b></div><div id="lane-unseen" class="lane-nodes"></div></section>
        <section class="lane"><div class="lane-title"><span>Learning</span><b id="lane-learning-count">0</b></div><div id="lane-learning" class="lane-nodes"></div></section>
        <section class="lane"><div class="lane-title"><span>Memorized</span><b id="lane-memorized-count">0</b></div><div id="lane-memorized" class="lane-nodes"></div></section>
      </div>
    </div>
    <section id="table-view" class="table-view" hidden aria-label="Vocabulary table"><table class="word-table"><thead><tr><th>Word</th><th>Definition</th><th>Status</th><th>Stability</th><th>Due</th></tr></thead><tbody id="table-body"></tbody></table></section>
    <div class="mode-switch"><button id="mode-atlas" class="mode-button" type="button" aria-pressed="true">Atlas</button><button id="mode-table" class="mode-button" type="button" aria-pressed="false">Table</button></div>
    <div class="zoom-controls"><button id="zoom-in" class="zoom-button" type="button" aria-label="Zoom in">+</button><button id="zoom-out" class="zoom-button" type="button" aria-label="Zoom out">−</button></div>
  </section>
  <aside id="detail" class="detail" aria-live="polite"><div class="empty-detail">Select a word</div></aside>
</div>
<footer><div class="legend"><span style="--tone:var(--new)">Unseen</span><span style="--tone:var(--learning)">Learning</span><span style="--tone:var(--memorized)">Memorized</span></div><span id="generated-at"></span></footer>
</div>
<script>
(function(){
"use strict";
var TOKEN_KEY="hermes-inspector-token";
var data=null,filter="all",sort="stability",selectedId=null,mode="atlas";
var scale=1,offsetX=0,offsetY=0,drag=null;
var byId=function(id){return document.getElementById(id)};
var gate=byId("gate"),app=byId("app"),gateError=byId("gate-error"),statusMessage=byId("status-message");
function node(tag,className,text){var value=document.createElement(tag);if(className)value.className=className;if(text!==undefined)value.textContent=text;return value}
function prettyStatus(value){return value.charAt(0).toUpperCase()+value.slice(1)}
function shortDate(value){if(value===null)return "—";return new Intl.DateTimeFormat("en",{day:"2-digit",month:"short",timeZone:"UTC"}).format(new Date(value))}
function days(value){return value===null?"—":value.toFixed(1)+" d"}
function firstDue(entry){var values=entry.cards.map(function(card){return card.effectiveDueAt}).sort();return values[0]||null}
function filteredEntries(){
  if(data===null)return [];
  var query=byId("search").value.trim().toLocaleLowerCase();
  var values=data.entries.filter(function(entry){
    if(filter!=="all"&&(filter==="due"?!entry.due:entry.status!==filter))return false;
    if(!query)return true;
    return entry.normalizedText.includes(query)||entry.senses.some(function(sense){return sense.definition.toLocaleLowerCase().includes(query)||sense.partOfSpeech.toLocaleLowerCase().includes(query)});
  });
  values.sort(function(left,right){
    if(sort==="alphabetical")return left.normalizedText.localeCompare(right.normalizedText);
    if(sort==="added")return right.dateAdded.localeCompare(left.dateAdded)||left.id-right.id;
    if(sort==="due")return (firstDue(left)||"9999").localeCompare(firstDue(right)||"9999")||left.id-right.id;
    return (right.weakestStability===null?-1:right.weakestStability)-(left.weakestStability===null?-1:left.weakestStability)||left.id-right.id;
  });
  return values;
}
function selectEntry(id){var restoreFocus=document.activeElement&&document.activeElement.hasAttribute("data-entry-id");selectedId=id;renderAtlas();renderTable();renderDetail();if(restoreFocus){var container=byId(mode==="atlas"?"atlas-view":"table-view"),target=container.querySelector('[data-entry-id="'+id+'"]');if(target)target.focus()}}
async function deleteEntry(entry,button,message){
  var token=sessionStorage.getItem(TOKEN_KEY);if(!token){setGate(true);return}
  button.disabled=true;button.textContent="Deleting…";message.textContent="";
  try{
    var response=await fetch("/admin/delete-entries",{method:"POST",headers:{Authorization:"Bearer "+token,"Content-Type":"application/json"},body:JSON.stringify({displayTexts:[entry.displayText]})});
    if(response.status===401){sessionStorage.removeItem(TOKEN_KEY);gateError.textContent="Invalid token";setGate(true);return}
    var result=await response.json();
    if(!response.ok||!Array.isArray(result.deleted)||!result.deleted.some(function(value){return value.id===entry.id}))throw new Error("Deletion failed");
    selectedId=null;if(await loadData())statusMessage.textContent='Deleted "'+entry.displayText+'".';
  }catch(error){message.textContent="Deletion failed · try again";button.disabled=false;button.textContent="Delete entry"}
}
function renderCounts(){
  ["total","unseen","learning","memorized"].forEach(function(key){byId("count-"+key).textContent=data.summary[key];if(key!=="total")byId("filter-"+key).textContent=data.summary[key]});
  byId("filter-all").textContent=data.summary.total;byId("filter-due").textContent=data.summary.due;byId("generated-at").textContent="Updated "+shortDate(data.generatedAt);
}
function renderAtlas(){
  var values=filteredEntries(),groups={unseen:[],learning:[],memorized:[]};
  values.forEach(function(entry){groups[entry.status].push(entry)});
  ["unseen","learning","memorized"].forEach(function(status){
    var lane=byId("lane-"+status);lane.replaceChildren();byId("lane-"+status+"-count").textContent=groups[status].length;
    groups[status].forEach(function(entry){
      var button=node("button","word-node"+(entry.due?" due":""));button.type="button";button.dataset.entryId=String(entry.id);button.dataset.status=entry.status;button.setAttribute("aria-current",String(entry.id===selectedId));button.style.setProperty("--lift",Math.max(2,Math.min(9,(entry.weakestStability||0)/8+2)).toFixed(1)+"px");
      button.append(node("strong","",entry.displayText),node("small","",entry.weakestStability===null?"NEW":days(entry.weakestStability)));button.addEventListener("click",function(event){event.stopPropagation();selectEntry(entry.id)});lane.append(button);
    });
  });
}
function renderTable(){
  var body=byId("table-body");body.replaceChildren();
  filteredEntries().forEach(function(entry){
    var row=node("tr");row.dataset.selected=String(entry.id===selectedId);var wordCell=node("td"),button=node("button","entry-button",entry.displayText);button.type="button";button.dataset.entryId=String(entry.id);button.addEventListener("click",function(){selectEntry(entry.id)});wordCell.append(button);
    var definition=entry.senses[0]?entry.senses[0].definition:"—";row.append(wordCell,node("td","definition-cell",definition),node("td","",prettyStatus(entry.status)),node("td","",days(entry.weakestStability)),node("td","",shortDate(firstDue(entry))));body.append(row);
  });
}
function renderDetail(){
  var detail=byId("detail"),entry=data&&data.entries.find(function(value){return value.id===selectedId});detail.replaceChildren();
  if(!entry){detail.append(node("div","empty-detail","Select a word"));return}
  var titleline=node("div","titleline"),titlebox=node("div"),title=node("h1","entry-title",entry.displayText),meta=node("div","meta","Added "+shortDate(entry.dateAdded)+(entry.lastReviewed?" · reviewed "+shortDate(entry.lastReviewed):"")),tag=node("span","tag",prettyStatus(entry.status)),tone=entry.status==="unseen"?"new":entry.status;tag.style.setProperty("--tone","var(--"+tone+")");titlebox.append(title,meta);titleline.append(titlebox,tag);detail.append(titleline);
  entry.senses.forEach(function(sense){var block=node("section","sense"),part=node("b","",sense.partOfSpeech),definition=node("p","",sense.definition);block.append(part,definition);if(sense.exampleSentence)block.append(node("div","example",sense.exampleSentence));detail.append(block)});
  var table=node("table","memory"),head=node("thead"),headRow=node("tr");["Card","Stability","Due"].forEach(function(label){headRow.append(node("th","",label))});head.append(headRow);var body=node("tbody");entry.cards.forEach(function(card){var row=node("tr"),senseIndex=entry.senses.findIndex(function(sense){return sense.id===card.senseId})+1,label=card.direction==="forward"?"Forward":"Reverse"+(senseIndex===0?"":" · "+senseIndex);row.append(node("td","",label),node("td","",days(card.stability)),node("td","",shortDate(card.effectiveDueAt)));body.append(row)});table.append(head,body);detail.append(table);
  if(entry.recentAttempts.length){var recent=node("section","recent"),heading=node("h3","","Recent attempts");recent.append(heading);entry.recentAttempts.forEach(function(attempt){var line=node("div","attempt");line.append(node("span","",shortDate(attempt.reviewedAt)+" · "+attempt.direction),node("b","",prettyStatus(attempt.rating)),node("span","",attempt.evaluatorGrade===null?"—":prettyStatus(attempt.evaluatorGrade)));recent.append(line)});detail.append(recent)}
  var danger=node("section","danger-zone"),dangerHeading=node("h3","","Maintenance"),dangerCopy=node("p","danger-copy","Permanently remove this entry, its cards, review history, and queued prompts."),deleteButton=node("button","delete-entry","Delete entry"),dangerStatus=node("div","danger-status");deleteButton.type="button";deleteButton.setAttribute("aria-describedby","delete-entry-help");dangerCopy.id="delete-entry-help";dangerStatus.setAttribute("role","status");deleteButton.addEventListener("click",function(){if(window.confirm('Delete "'+entry.displayText+'" permanently?\\n\\nThis removes its senses, cards, review history, and queued prompts. This cannot be undone.'))deleteEntry(entry,deleteButton,dangerStatus)});danger.append(dangerHeading,dangerCopy,deleteButton,dangerStatus);detail.append(danger);
}
function render(){if(selectedId===null&&data.entries.length)selectedId=data.entries[0].id;renderCounts();renderAtlas();renderTable();renderDetail()}
function setMode(next){mode=next;byId("atlas-view").hidden=mode!=="atlas";byId("table-view").hidden=mode!=="table";byId("mode-atlas").setAttribute("aria-pressed",String(mode==="atlas"));byId("mode-table").setAttribute("aria-pressed",String(mode==="table"));byId("mode-table-top").textContent=mode==="atlas"?"Table":"Atlas";byId("zoom-in").hidden=mode!=="atlas";byId("zoom-out").hidden=mode!=="atlas"}
function transformWorld(){byId("atlas-world").style.transform="translate("+offsetX+"px,"+offsetY+"px) scale("+scale+")"}
function setGate(visible){gate.hidden=!visible;app.hidden=visible;if(visible)requestAnimationFrame(function(){byId("token").focus()})}
async function loadData(){
  var token=sessionStorage.getItem(TOKEN_KEY);if(!token){setGate(true);return false}
  gateError.textContent="";statusMessage.textContent="Loading…";
  try{
    var response=await fetch("/admin/inspector-data",{headers:{Authorization:"Bearer "+token}});
    if(response.status===401){sessionStorage.removeItem(TOKEN_KEY);gateError.textContent="Invalid token";setGate(true);return false}
    if(!response.ok)throw new Error("Request failed with "+response.status);
    data=await response.json();statusMessage.textContent="";setGate(false);render();return true;
  }catch(error){statusMessage.textContent=data===null?"Could not load vocabulary":"Refresh failed · try again";if(data===null){gateError.textContent="Could not load vocabulary";setGate(true)}return false}
}
byId("token-form").addEventListener("submit",function(event){event.preventDefault();sessionStorage.setItem(TOKEN_KEY,byId("token").value);loadData()});
byId("refresh").addEventListener("click",loadData);byId("search").addEventListener("input",function(){renderAtlas();renderTable()});byId("sort").addEventListener("change",function(event){sort=event.target.value;renderAtlas();renderTable()});
document.querySelectorAll("[data-filter]").forEach(function(button){button.addEventListener("click",function(){filter=button.dataset.filter;document.querySelectorAll("[data-filter]").forEach(function(item){item.setAttribute("aria-pressed",String(item===button))});renderAtlas();renderTable()})});
byId("mode-atlas").addEventListener("click",function(){setMode("atlas")});byId("mode-table").addEventListener("click",function(){setMode("table")});byId("mode-table-top").addEventListener("click",function(){setMode(mode==="atlas"?"table":"atlas")});
byId("zoom-in").addEventListener("click",function(){scale=Math.min(1.8,scale+.15);transformWorld()});byId("zoom-out").addEventListener("click",function(){scale=Math.max(.55,scale-.15);transformWorld()});
var atlas=byId("atlas-view");atlas.addEventListener("wheel",function(event){event.preventDefault();scale=Math.max(.55,Math.min(1.8,scale-event.deltaY*.001));transformWorld()},{passive:false});atlas.addEventListener("pointerdown",function(event){if(event.target.closest(".word-node"))return;drag={x:event.clientX-offsetX,y:event.clientY-offsetY};atlas.setPointerCapture(event.pointerId);atlas.classList.add("dragging")});atlas.addEventListener("pointermove",function(event){if(!drag)return;offsetX=event.clientX-drag.x;offsetY=event.clientY-drag.y;transformWorld()});atlas.addEventListener("pointerup",function(){drag=null;atlas.classList.remove("dragging")});atlas.addEventListener("pointercancel",function(){drag=null;atlas.classList.remove("dragging")});
setMode("atlas");loadData();
})();
</script>
</body>
</html>`;
