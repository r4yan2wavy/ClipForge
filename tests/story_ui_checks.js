/* Run: node tests/story_ui_checks.js. No browser or live requests. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "..");
const script = fs.readFileSync(path.join(root, "clipforge/web_static/app.js"), "utf8");
const markup = fs.readFileSync(path.join(root, "clipforge/web_static/index.html"), "utf8");
assert(markup.includes('data-page="stories"'));
assert(markup.includes('id="page-stories"'));
assert(!markup.includes('http://cdn') && !markup.includes('https://cdn'), "The story UI must not add hosted dependencies");
const nodes = new Map();
function element() {
  const fields = new Map();
  return {value:"", innerHTML:"", textContent:"", disabled:false, hidden:false, style:{}, dataset:{}, attributes:new Map(),
    classList:{toggle(){}}, elements:new Proxy({}, {get(_t,key){if(!fields.has(key))fields.set(key,element());return fields.get(key);}}),
    querySelectorAll(){return [];}, querySelector(){return element();}, addEventListener(){},
    setAttribute(key,value){this.attributes.set(key,value);},getAttribute(key){return this.attributes.get(key);},removeAttribute(key){this.attributes.delete(key);},
    append(){},pause(){this.paused=true;},load(){this.loaded=true;},focus(){},reportValidity(){return true;},
  };
}
function node(id){if(!nodes.has(id)) nodes.set(id,element());return nodes.get(id);}
let replies=[];
let requests=[];
const context = vm.createContext({document:{querySelector:node,querySelectorAll(){return [];},createElement:element},window:{addEventListener(){},confirm(){return false;}}, location:{origin:"http://localhost:8000",hash:"#stories"},URL,Number,String,Math,Intl,setTimeout(){},clearTimeout(){},fetch:async(url,options)=>{requests.push({url,options}); if(!replies.length)throw Error("Unexpected UI request: "+url);return {ok:true,json:async()=>replies.shift()};}});
vm.runInContext(script.replace(/\ninit\(\);\s*$/, ""),context);
const run = code=>vm.runInContext(code,context);
async function checks(){
  run('state.connected=true;storyState.capabilities={ok:false,ollama_ready:false,voices:[]};renderStoryCapabilities()');
  assert.equal(node("#story-generate").disabled,true);
  assert.equal(node("#story-save").disabled,false,"Drafts should remain saveable before setup is complete");
  run('storyState.capabilities={ok:true,ollama_ready:true,voices:[{id:"sapi:voice",name:"Local voice",provider:"sapi"}]};renderStoryCapabilities()');
  assert.equal(node("#story-generate").disabled,false);
  assert.match(node("#story-voice").innerHTML,/Local voice/);
  run('storyState.project={id:"project1",title:"Bad <script>",options:{quality:720},scenes:[{id:"s1",narration:"No <script> here",character_ids:["hero"],setting:"bedroom",action:"look",camera:"zoom",lighting:"normal",effect:"none"}],characters:[{id:"hero",name:"<img>"}]};renderStoryEditor();renderStoryOutput()');
  assert.match(node("#story-scenes").innerHTML,/No &lt;script&gt; here/);
  assert(!node("#story-scenes").innerHTML.includes("No <script>"));
  assert.match(node("#story-characters").innerHTML,/&lt;img&gt;/);
  assert.match(node("#story-scenes").innerHTML,/data-scene-field="backdrop"/);
  assert.match(node("#story-scenes").innerHTML,/<option value="night" selected>/);
  assert.equal(node("#story-output-title").textContent,"Bad <script>");
  run('storyState.project.video_url="https://outside.example/video.mp4";renderStoryOutput()');
  assert.equal(node("#story-video").hidden,true,"Preview rejects remote media links");
  run('storyState.project.video_url="/media/clip/abc";storyState.project.clip_id="abc";renderStoryOutput()');
  assert.equal(node("#story-video").hidden,false);
  assert.equal(node("#story-download").href,"/media/clip/abc?download=1");
  run('storyState.project.export_available=false;renderStoryOutput()');
  assert.equal(node("#story-video").hidden,true,"Deleted exports must not keep a dangling preview");
  run('storyState.project.export_available=true');
  run('state.jobs=[{id:"job1",project_id:"project1",kind:"story",state:"running",stage:"story_rendering",can_cancel:true,stage_percent:40,elapsed_s:20}];renderStoryOutput()');
  assert.equal(node("#story-generate").disabled,true);
  assert.equal(node("#story-cancel").hidden,false);
  assert.match(node("#story-progress").innerHTML,/aria-valuenow="40"/);
  run('state.jobs[0].stage_percent=null;renderStoryOutput()');
  assert.match(node("#story-progress").innerHTML,/progress-track indeterminate/);
  run('state.jobs[0].state="completed";renderStoryOutput()');
  assert.equal(node("#story-cancel").hidden,true);
  assert.equal(node("#story-generate").disabled,false);
  run('storyState.ideasJob="ideas1";state.jobs=[{id:"ideas1",state:"completed",result:{ideas:["A door <opens>","A mirror speaks"]}}];renderStoryIdeas()');
  assert.match(node("#story-idea-results").innerHTML,/A door &lt;opens&gt;/);
  const sceneElement=element();
  sceneElement.dataset.storyScene="s1";
  const backdropField=element();backdropField.dataset.sceneField="backdrop";backdropField.value="forest";
  const narrationField=element();narrationField.dataset.sceneField="narration";narrationField.value="The forest moved.";
  sceneElement.querySelectorAll=()=>[backdropField,narrationField];
  node("#story-scenes").querySelectorAll=()=>[sceneElement];
  assert.equal(run('storySceneOptions()[0].backdrop'),"forest");
  assert.equal(run('storySceneOptions()[0].character_ids[0]'),"hero");
  node("#story-scenes").querySelectorAll=()=>[];
  const options=node("#story-form").elements;
  Object.entries({idea:"My draft",genre:"mystery",duration:"60",quality:"720",voice:"",captions:"true",music:"false",rate:"0"}).forEach(([k,v])=>options[k].value=v);
  run('storyState.project=null;storyState.dirty=true');
  replies=[{project:{id:"draft",updated_at:"1",options:{idea:"My draft",voice:"",duration:60,quality:720}}}];
  await run('saveStory()');
  assert.equal(requests.at(-1).url,"/api/stories");
  assert.equal(JSON.parse(requests.at(-1).options.body).voice,"");
  assert.equal(run("storyState.dirty"),false);
  run('storyState.dirty=true;storyFields().idea.value="unsaved local edit"');
  replies=[{projects:[{id:"draft",updated_at:"2",state:"completed"}],capabilities:{ok:true,ollama_ready:true,voices:[]}}];
  const before=requests.length;
  await run('refreshStories()');
  assert.equal(requests.length,before+1,"Polling must not fetch/replace a dirty project");
  assert.equal(options.idea.value,"unsaved local edit");
  run('state.jobs=[];storyState.project={id:"draft",options:{idea:"Saved idea"},scenes:[],updated_at:"1"};storyState.capabilities={ok:true,ollama_ready:true,voices:[]};storyState.busy=false');
  Object.entries({idea:"A different world",genre:"mystery",duration:"60",quality:"720",voice:"sapi:voice",captions:"true",music:"true",rate:"0",batch_count:"3"}).forEach(([k,v])=>options[k].value=v);
  assert(!Object.hasOwn(run('storyOptions()'),"batch_count"),"Batch controls must not leak into persistent story options");
  const batchProject={id:"batch1",title:"The First World",options:{idea:"A different world",duration:60,quality:720,voice:"sapi:voice"},scenes:[],state:"queued",updated_at:"3"};
  replies=[{project:{id:"draft",options:{idea:"A different world"},scenes:[],updated_at:"2"}}, {projects:[batchProject],jobs:[]}, {jobs:[{id:"batch-job",kind:"story",state:"queued",project_id:"batch1"}],clips:[],sources:[],links:[]}, {projects:[batchProject],capabilities:{ok:true,ollama_ready:true,voices:[]}}];
  const batchStart=requests.length;
  await run('storyAction("generate",document.querySelector("#story-generate"))');
  const batchRequests=requests.slice(batchStart);
  assert.equal(batchRequests[0].url,"/api/stories/draft/save","Batch generation must save the edited original first");
  assert.equal(batchRequests[1].url,"/api/stories/batch");
  const batchBody=JSON.parse(batchRequests[1].options.body);
  assert.equal(batchBody.count,3);
  assert.equal(batchBody.options.idea,"A different world");
  assert(!Object.hasOwn(batchBody.options,"batch_count"));
  assert.equal(run('storyState.project.id'),"batch1","The first batch project should be selected for progress");
  assert.equal(options.batch_count.value,"1","Reopening a project should default to a single render");
  run('state.jobs=[]');
  run('storyState.project={id:"a",title:"<img>"};storyState.projects=[storyState.project];storyState.dirty=true;renderStoryHistory()');
  assert.match(node("#story-history").innerHTML,/&lt;img&gt;/);
  const beforeSwitch=requests.length;
  await run('openStory("other")');
  assert.equal(requests.length,beforeSwitch,"Rejected switch must not fetch another project");
  assert.equal(run("storyState.project.id"),"a","Rejected unsaved-change prompt must not switch projects");
  console.log("Story UI checks passed: setup gates, local preview, escaped storyboard/history, draft save, real progress, cancel state, batch queuing and unsaved-change preservation.");
}
checks().catch(error=>{console.error(error);process.exitCode=1;});
