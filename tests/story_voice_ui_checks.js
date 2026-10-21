/* Run: node tests/story_voice_ui_checks.js. Exercises real UI logic with fake local responses. */
"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
const root=path.resolve(__dirname,".."),source=fs.readFileSync(path.join(root,"clipforge/web_static/app.js"),"utf8");
const nodes=new Map();
function element(){const fields=new Map();return {value:"",innerHTML:"",textContent:"",disabled:false,hidden:false,open:false,style:{},dataset:{},attributes:new Map(),children:[],
classList:{toggle(){}},elements:new Proxy({},{get(_t,k){if(!fields.has(k))fields.set(k,element());return fields.get(k);}}),
querySelectorAll(){return [];},querySelector(){return element();},addEventListener(){},setAttribute(k,v){this.attributes.set(k,v);},getAttribute(k){return this.attributes.get(k);},removeAttribute(k){this.attributes.delete(k);},append(...c){this.children.push(...c);},pause(){},load(){},showModal(){this.open=true;},play(){return Promise.resolve();}};}
function node(id){if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);}
let replies=[],requests=[];
const context=vm.createContext({document:{querySelector:node,querySelectorAll(){return [];},createElement:element},window:{addEventListener(){}},location:{origin:"http://localhost:8000",hash:"#stories"},URL,Number,String,Math,Intl,setTimeout(){},clearTimeout(){},fetch:async(url,options)=>{requests.push({url,options});assert(replies.length,"Unexpected request "+url);return {ok:true,json:async()=>replies.shift()};}});
vm.runInContext(source.replace(/\ninit\(\);\s*$/,""),context);
const run=code=>vm.runInContext(code,context);
function field(name,value){const e=element();e.dataset.beatField=name;e.value=value;return e;}
async function check(){
run('state.connected=true;storyState.capabilities={ok:true,ollama_ready:true,voices:[{id:"sapi:local",name:"Local"}]};state.jobs=[{id:"i",kind:"story_voice_install",state:"running",voice_catalog_id:"en-local",can_cancel:true,stage_percent:33}];renderStoryControls()');
assert.equal(node("#story-generate").disabled,false,"Voice installs must not lock story forms without a project");
node("#story-form").elements.voice.value="sapi:local";
run('storyVoiceState.loaded=true;storyVoiceState.engineReady=true;storyVoiceState.voices=[{id:"sapi:local",name:"Name <img>",provider:"sapi",language:"en-US"}];storyVoiceState.catalog=[{id:"en-local",name:"Neural <script>",provider:"piper",language:"en-GB",download_bytes:62000000,license:"MIT <terms>",license_url:"https://example.com/license",source_url:"javascript:alert(1)",installed:false}];renderStoryVoiceLibrary()');
assert.match(node("#story-installed-voices").innerHTML,/Name &lt;img&gt;/);
const catalog=node("#story-voice-catalog").innerHTML;
assert.match(catalog,/Neural &lt;script&gt;/);assert.match(catalog,/en-GB/);assert.match(catalog,/59.1 MB/);assert.match(catalog,/MIT &lt;terms&gt;/);assert(!catalog.includes("javascript:"));
assert.match(catalog,/aria-valuenow="33"/);assert.match(catalog,/data-voice-job-cancel="i"/);
run('requestStoryVoiceCancel("i")');assert.equal(node("#cancel-job-form").elements.job_id.value,"i");assert.equal(node("#cancel-job-dialog").open,true);
run('storyVoiceState.previewJob="p";state.jobs=[{id:"p",kind:"story_voice_preview",state:"completed",voice_id:"sapi:local",voice_preview_url:"/media/story-voice/p"}];renderStoryVoicePreview()');
assert.equal(node("#story-voice-audio").hidden,false);assert.equal(node("#story-voice-audio").src,"/media/story-voice/p");
run('state.jobs[0].voice_preview_url="https://outside.example/audio.wav";renderStoryVoicePreview()');assert.equal(node("#story-voice-audio").hidden,true,"Preview must reject external audio");
run('storyState.project={id:"story",options:{idea:"unsaved"},scenes:[{id:"scene_01",narration:"A mystery.",character_ids:["hero"],prop:"key",beats:[{at:.2,character_id:"hero",action:"pick_up",target:"prop",expression:"curious"}]}]};storyState.dirty=true;storyFields().idea.value="Unsaved idea"');
replies=[{voices:[{id:"sapi:local",name:"Local",provider:"sapi",language:"en-US"}],catalog:[],engine_ready:true,engine_message:"Ready"}];
await run('refreshStoryVoiceLibrary()');
assert.equal(node("#story-form").elements.idea.value,"Unsaved idea");assert.equal(run('storyState.dirty'),true);assert.equal(node("#story-form").elements.voice.value,"sapi:local");
const scene=element();scene.dataset.storyScene="scene_01";
scene.querySelectorAll=()=>[];
node("#story-scenes").querySelectorAll=()=>[scene];
assert.equal(run('storySceneOptions()[0].prop'),"key");assert.equal(run('storySceneOptions()[0].beats[0].action'),"pick_up","Scene saves must retain choreography not edited in the UI");
scene.dataset.motionEdited="true";assert.equal(run('storySceneOptions()[0].beats.length'),0,"Cleared choreography should stay cleared");
const beat=element();beat.querySelectorAll=()=>[field("at","45"),field("character_id","hero"),field("action","open"),field("target","window"),field("expression","surprised")];
scene.querySelectorAll=selector=>selector==="[data-story-beat]"?[beat]:[];
assert.equal(run('storySceneOptions()[0].beats[0].at'),.45);assert.equal(run('storySceneOptions()[0].beats[0].expression'),"surprised");
node("#story-scenes").querySelectorAll=()=>[];
run('renderStoryEditor()');assert.match(node("#story-scenes").innerHTML,/value="pick_up"/);assert.match(node("#story-scenes").innerHTML,/data-beat-field="at"/);assert.match(node("#story-scenes").innerHTML,/value="key" selected/);
run('state.jobs=[];storyState.project=null;storyVoiceState.previewJob=null');
replies=[{job:{id:"preview1"}},{jobs:[{id:"preview1",kind:"story_voice_preview",voice_id:"sapi:local",state:"queued",can_cancel:true}],sources:[],clips:[],links:[]}];
await run('previewStoryVoice("sapi:local")');
assert.equal(requests.at(-2).url,"/api/story-voices/preview");assert.equal(JSON.parse(requests.at(-2).options.body).voice,"sapi:local");assert.equal(run('storyVoiceState.previewJob'),"preview1");assert.equal(node("#story-voice-preview-cancel").hidden,false);
run('state.jobs=[];storyVoiceState.loaded=false');
replies=[{job:{id:"install1"}},{jobs:[{id:"install1",kind:"story_voice_install",voice_catalog_id:"catalog-voice",state:"queued",can_cancel:true}],sources:[],clips:[],links:[]}];
await run('installStoryVoice("catalog-voice")');
assert.equal(requests.at(-2).url,"/api/story-voices/install");assert.equal(JSON.parse(requests.at(-2).options.body).id,"catalog-voice");
console.log("Story voice UI checks passed: local previews, catalog terms/progress, install/cancel requests, independent story controls and preserved motion beats.");
}
check().catch(e=>{console.error(e);process.exitCode=1;});
