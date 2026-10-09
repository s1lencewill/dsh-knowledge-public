import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../ui/source-card.js',import.meta.url),'utf8');
function harness(){
 let module; const entries=[],effects=[],opens=[]; let mounted='session-a', disposed=0;
 const React={useRef:v=>({current:v}),useEffect:()=>{},createElement:(type,props,...children)=>({type,props:props||{},children:children.flat(Infinity)}),useState:v=>[v,()=>{}],useSyncExternalStore:(_subscribe,snapshot)=>snapshot()};
 new Function('window',source)({__ModuleLoader__:{load:spec=>module=spec.factory(name=>{assert.equal(name,'react');return React;})}});
 const slots={inject:(name,fn)=>{for(const e of fn())entries.push({name,...e});},register:(spec,Component)=>({spec,Component})};
 const child={slots,effect:fn=>effects.push(fn()),sidebarRight:{mounted:{getSnapshot:()=>mounted},openTab:kind=>opens.push(kind)},sidebarRightTabs:{register:definition=>{assert.equal(definition.kind,'dsh-knowledge.sources');return ()=>disposed++;}}};
 module.apply({slots,inject:(needs,fn)=>{assert.deepEqual(needs,['sidebarRightTabs','sidebarRight']);fn(child);}});
 function render(Component,props){const nodes=[],texts=[];function walk(n){if(n==null||typeof n==='boolean')return;if(Array.isArray(n))return n.forEach(walk);if(typeof n!=='object'){texts.push(String(n));return;}if(typeof n.type==='function')return walk(n.type(n.props));nodes.push(n);walk(n.children);}walk(Component(props));return {nodes,text:texts.join('\n')};}
 const Card=entries.find(e=>e.spec.key==='knowledge_read').Component,Panel=entries.find(e=>e.spec.key==='dsh-knowledge.sources').Component;
 const card={title:'Source A',libraryId:'lib-a',fileName:'a.md',excerpt:'Unique source A',sourceVersion:'abc',location:{kind:'text-lines',startLine:1,endLine:2},status:{identity:'undetermined',support:'not-assessed'}};
 const block={kind:'tool-result',call:{name:'knowledge_read'},content:[],meta:{sources:[card]}};
 const button=(r,label)=>{const b=r.nodes.find(n=>n.type==='button'&&n.children.some(c=>String(c).includes(label)));assert.ok(b);return b;};
 return {render,Card,Panel,block,button,opens,setMounted:v=>mounted=v,dispose:()=>effects.forEach(f=>f()),disposed:()=>disposed};
}
test('rightbar opens returned source without making any request and stays session scoped',()=>{
 const h=harness();const card=h.render(h.Card,{block:h.block,sessionId:'session-a'});h.button(card,'在右栏查看来源').props.onClick();
 assert.deepEqual(h.opens,['dsh-knowledge.sources']);assert.match(h.render(h.Panel,{sessionId:'session-a'}).text,/Unique source A/);
 assert.doesNotMatch(h.render(h.Panel,{sessionId:'session-b'}).text,/Unique source A/);
 h.setMounted('session-b');h.button(card,'在右栏查看来源').props.onClick();assert.equal(h.opens.length,1);
});
test('unload unregisters tab and clears the ephemeral source snapshot',()=>{
 const h=harness();const old=h.button(h.render(h.Card,{block:h.block,sessionId:'session-a'}),'在右栏查看来源');old.props.onClick();h.dispose();assert.equal(h.disposed(),1);old.props.onClick();assert.equal(h.opens.length,1);
 assert.match(h.render(h.Panel,{sessionId:'session-a'}).text,/不可用/);
 const fresh=harness();assert.match(fresh.render(fresh.Panel,{sessionId:'session-a'}).text,/尚未选择/);
});
test('library panel describes a past tool result rather than live service health',()=>{
 const h=harness();const block={kind:'tool-result',call:{name:'knowledge_list_libraries'},content:[{type:'text',text:JSON.stringify({libraries:[{id:'lib-a'}],selectedLibraryId:'lib-a'})}],meta:{sources:[]}};
 const r=h.render(h.Card,{block,sessionId:'session-a'});h.button(r,'在右栏查看知识库状态').props.onClick();const panel=h.render(h.Panel,{sessionId:'session-a'});
 assert.match(panel.text,/lib-a/);assert.match(panel.text,/历史列库/);
});
test('host-persisted failure facts survive absent meta without displaying hostile fields',()=>{
 const h=harness();const facts={code:'KNOWLEDGE_SERVICE_UNREACHABLE',reference:'k-abcd1234',detail:{used:2,max:6,secret:'sk-private'},budget:{used:2,max:6}};
 const block={kind:'tool-result',call:{name:'knowledge_read'},callId:'call-failure',isError:true,error:{code:facts.code},content:[{type:'text',text:'Error: KNOWLEDGE_FAILURE_FACTS='+JSON.stringify(facts)}]};
 const result=h.render(h.Card,{block,sessionId:'session-a'});assert.match(result.text,/2/);assert.match(result.text,/6/);assert.match(result.text,/k-abcd1234/);assert.doesNotMatch(result.text,/sk-private/);
});
