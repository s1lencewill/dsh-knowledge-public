import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../ui/source-card.js',import.meta.url),'utf8');
function harness(){let exports,offset=0;const states=[],refs=[],effects=[];const React={createElement:(type,props,...children)=>({type,props:props||{},children:children.flat(Infinity)}),useState:initial=>{const i=offset++;if(!(i in states))states[i]=initial;return [states[i],v=>{states[i]=typeof v==='function'?v(states[i]):v;}];},useRef:v=>({current:v}),useEffect:(fn,deps)=>effects.push({fn,deps})};
 const code=source.replace('exports.apply = apply;','exports.readerSegments=readerSegments; exports.Workbench=Workbench; exports.filterSources=filterSources; exports.sourcePreview=sourcePreview; exports.excerptKey=excerptKey; exports.PanelControls=PanelControls; exports.apply=apply;');
 new Function('window',code)({__ModuleLoader__:{load:spec=>exports=spec.factory(()=>React)}});
 function render(component,props){offset=0;const root=component(props);const nodes=[];function walk(n){if(n&&typeof n==='object'){nodes.push(n);n.children?.forEach(walk);}}walk(root);return {root,nodes,text:JSON.stringify(root,(key,v)=>typeof v==='function'?undefined:v)};}
 return {exports,render,states,effects};}
const card={libraryId:'wiki',title:'方法说明',fileName:'note.md',sourceVersion:'v1',excerpt:'四点法的真实原文。\n<script>do not execute</script>',location:{startLine:4,endLine:7},status:{identity:'undetermined',locationVerified:true}};
const tree={groups:[{library:'wiki',documents:[{key:'doc1',title:card.title,cards:[card]}]}],failures:[],pending:0,budget:{used:2,max:6}};
const props={sessionId:'session-a',active:true,tree,value:{kind:'source',card},runtime:{store:{select(){}}}};
const button=(r,name)=>r.nodes.find(n=>n.type==='button'&&n.children.includes(name));
test('source filter and preview are literal local text, not generated conclusions',()=>{const h=harness();assert.equal(h.exports.filterSources(tree.groups,'四点法').length,1);assert.equal(h.exports.filterSources(tree.groups,'absent').length,0);assert.equal(h.exports.sourcePreview(card),'四点法的真实原文。 <script>do not execute</script>');});
test('selecting, filtering, resizing and expanding dispatch no network or tools',()=>{const h=harness();let selected;props.runtime.store.select=(id,value)=>selected={id,value};const old=globalThis.fetch;globalThis.fetch=()=>{throw Error('No request allowed');};try{let r=h.render(h.exports.Workbench,props);r.nodes.find(n=>n.props.className==='kw-excerpt').props.onClick();assert.equal(selected.id,'session-a');assert.equal(selected.value.card,card);button(r,'展开阅读').props.onClick();r=h.render(h.exports.Workbench,props);assert.ok(button(r,'返回来源树'));assert.ok(!r.nodes.some(n=>n.props.className==='kw-sources'));button(r,'返回来源树').props.onClick();r=h.render(h.exports.Workbench,props);const divider=r.nodes.find(n=>n.props.role==='separator');divider.props.onKeyDown({key:'ArrowDown',preventDefault(){}});assert.equal(h.states[1],40);r.nodes.find(n=>n.type==='input').props.onChange({target:{value:'missing'}});r=h.render(h.exports.Workbench,props);assert.match(r.text,/没有匹配/);assert.match(r.text,/四点法的真实原文/);}finally{globalThis.fetch=old;}});
test('hidden session cannot replace selected source; returned text stays inert',()=>{const h=harness();let calls=0;const p={...props,active:false,runtime:{store:{select(){calls++;}}}};const r=h.render(h.exports.Workbench,p);r.nodes.find(n=>n.props.className==='kw-excerpt').props.onClick();assert.equal(calls,0);assert.ok(!r.nodes.some(n=>n.props.dangerouslySetInnerHTML));assert.match(r.text,/<script>/);assert.ok(!r.nodes.some(n=>['iframe','img','script'].includes(n.type)));});
test('EOF and truncated statements remain distinct, neither offers automatic reading',()=>{const h=harness();let r=h.render(h.exports.Workbench,{...props,value:{kind:'source',card:{...card,truncationNotices:['EXCERPT_INCOMPLETE_AT_EOF']}}});assert.match(r.text,/已到文件末尾/);assert.doesNotMatch(r.text,/可能缺少条件或单位/);r=h.render(h.exports.Workbench,{...props,value:{kind:'source',card:{...card,truncationNotices:['EXCERPT_TRUNCATED']}}});assert.match(r.text,/可能缺少条件或单位/);assert.ok(!r.nodes.some(n=>n.type==='button'&&n.children.includes('继续读取')));});
test('settings are initially closed, Escape closes drawer and next-turn scope is explicit',()=>{const h=harness();let r=h.render(h.exports.PanelControls,{sessionId:'session-a',active:false});assert.ok(!r.nodes.some(n=>n.props.role==='dialog'));r.nodes.find(n=>n.props['aria-label']==='知识库设置').props.onClick();r=h.render(h.exports.PanelControls,{sessionId:'session-a',active:false});assert.match(r.text,/下一轮生效/);assert.match(r.text,/当前轮保持原上限/);r.nodes.find(n=>n.props.role==='dialog').props.onKeyDown({key:'Escape'});assert.ok(!h.render(h.exports.PanelControls,{sessionId:'session-a',active:false}).nodes.some(n=>n.props.role==='dialog'));});
test('new sources do not change selected card or reader effect dependency',()=>{const h=harness();const before=h.render(h.exports.Workbench,props);const dep=h.effects.at(-1).deps[0];const after=h.render(h.exports.Workbench,{...props,tree:{...tree,groups:[...tree.groups,{library:'other',documents:[{key:'new',title:'New',cards:[{...card,title:'New',fileName:'other.md'}]}]}]}});assert.equal(h.effects.at(-1).deps[0],dep);assert.equal(before.nodes.find(n=>n.props['aria-pressed']===true).children[0],after.nodes.find(n=>n.props['aria-pressed']===true).children[0]);});

test('code, math and table blocks scroll separately without losing characters or interpreting HTML',()=>{const h=harness();const input='intro\n```js\n<script>literal</script>\n```\n|value|unit|\n|3.0|M/atm|\n$$x=1$$\noutro';const parts=h.exports.readerSegments(input);assert.equal(parts.map(p=>p.text).join(''),input);assert.equal(parts.filter(p=>p.block).length,3);});

test('copy preserves the exact excerpt and separates provenance from document text',async()=>{
 const h=harness(),written=[],oldNavigator=Object.getOwnPropertyDescriptor(globalThis,'navigator');
 Object.defineProperty(globalThis,'navigator',{configurable:true,value:{clipboard:{writeText:async value=>written.push(value)}}});
 const oldFetch=globalThis.fetch;globalThis.fetch=()=>{throw Error('Clipboard must not dispatch a request');};
 try{
  const r=h.render(h.exports.Workbench,props);
  await button(r,'复制摘录').props.onClick();
  await button(r,'复制出处').props.onClick();
  assert.equal(written[0],card.excerpt);
  assert.match(written[1],/方法说明/);assert.doesNotMatch(written[1],/sourceVersion|v1/);
  assert.doesNotMatch(written[1],/<script>/);assert.doesNotMatch(written[1],/kref-|krh1\.|Bearer/);
 }finally{globalThis.fetch=oldFetch;if(oldNavigator)Object.defineProperty(globalThis,'navigator',oldNavigator);else delete globalThis.navigator;}
});
