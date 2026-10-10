/**
 * dsh-knowledge browser half: the source card for the knowledge tools.
 *
 * This file is the tracked SOURCE of the served artifact; `scripts/build-client.mjs` injects the frozen build identity into
 * `lib/browser.js`, which `exports["./client"].default` points at. It is hand-authored in the exact
 * container format the shell's module loader expects
 * (`window.__ModuleLoader__.load({id, factory})`, lazy-CJS), so no bundler and no new dependency is
 * needed. Two facts from the host were verified by probe before writing this (see
 * `docs/m0-host-integration.md`): a `dsh.client` package may resolve `react` and
 * `@deepseek-ai/dsh-client-ui-slots` against the shell's seeded module table, and `tool.call.toolview`
 * is the official keyed slot for owning one tool's conversation card.
 *
 * What this card deliberately does NOT do:
 *
 * - it never contacts the knowledge service. The panel holds no token; status and settings use the authenticated host bridge, so there is
 *   nothing here that could leak the Bearer token into the page, and opening a card cannot make the
 *   service do work (no model call, no read, no quota charge);
 * - it never injects anything into the conversation. Expanding a card is display only; loading more
 *   context is the main Agent's `knowledge_read` call, and the card says so;
 * - it never renders source text as HTML. Every string goes through React text children, so markup or a
 *   script inside a document stays inert text;
 * - it never invents a PDF page, a version, a verification status, or a "verified" badge. It renders the
 *   fields the service returned, and says "未核验 / 未评估" where the service said nothing.
 */
window.__ModuleLoader__.load({
  id: "dsh-knowledge",
  factory: (require) => {
    const clientBuild = '__KNOWLEDGE_CLIENT_BUILD_ID__';
    const fetch = (input, options = {}) => {
      const headers = new Headers(options.headers);
      if (/^[a-f0-9]{64}$/.test(clientBuild)) headers.set('X-Knowledge-Client-Build', clientBuild);
      return globalThis.fetch(input, {...options, headers});
    };
    var module = { exports: {} };
    var exports = module.exports;
    Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
    const React = require("react");
    const h = React.createElement;

    // Optional official rightbar integration. A snapshot exists only for an explicit click;
    // no text is persisted into navigation params, browser storage or a URL.
    let panelRuntime = null;
    const PANEL_KIND = 'dsh-knowledge.sources';
    const UI_BUILD = 'workbench-polish-v1';
    let diagnosticSequence=0;
    function panelDiagnostic(code,phase){
      const record={code,build:UI_BUILD,phase,correlationId:'kw-'+(++diagnosticSequence).toString(16)};
      // Never pass Error, stack, event, message or source text to the console.
      if(typeof console!=='undefined')console.warn('knowledge-panel',record);
      return record;
    }
    const runtimeListeners=new Set();
    const subscribeRuntime=listener=>{runtimeListeners.add(listener);return()=>runtimeListeners.delete(listener);};
    function notifyRuntime(){for(const listener of [...runtimeListeners])try{listener();}catch{panelDiagnostic('PANEL_LISTENER_FAILED','runtime');}}
    function safeStrings(value){return Array.isArray(value)?value.filter(v=>typeof v==='string').slice(0,32):[];}
    function normalizeParseQuality(value){
      if(value==null)return {value:null,degraded:false};
      if(typeof value==='string')return value.length<=512&&[...value].length<=256?{value:value.trim()||null,degraded:false}:{value:null,degraded:true};
      if(typeof value!=='object'||Array.isArray(value)||Object.getPrototypeOf(value)!==Object.prototype)return {value:null,degraded:true};
      const clean={};
      for(const key of ['privateUseChars','replacementChars','searchableCjkRuns'])if(Object.hasOwn(value,key)){const n=value[key];if(n!==null&&(!Number.isSafeInteger(n)||n<0))return {value:null,degraded:true};clean[key]=n;}
      if(Object.hasOwn(value,'reviewPages')){const pages=value.reviewPages;if(pages===null)clean.reviewPages=null;else {if(!Array.isArray(pages)||pages.length>256||pages.some(n=>!Number.isSafeInteger(n)||n<1||n>100000))return {value:null,degraded:true};clean.reviewPages=[...new Set(pages)].sort((a,b)=>a-b);}}
      return {value:clean,degraded:false};
    }
    function formatParseQuality(value){const q=normalizeParseQuality(value).value;if(q===null)return '未记录';if(typeof q==='string')return q;const count=(key)=>q[key]==null?'未记录':String(q[key]);return '替换字符 '+count('replacementChars')+'；私用区字符 '+count('privateUseChars')+'；'+(q.reviewPages==null?'待复核页未记录':q.reviewPages.length?'待复核页 '+q.reviewPages.join('、'):'未标注待复核页')+'；连续中文串 '+count('searchableCjkRuns');}
    function sourceCard(value){
      if(!value||typeof value!=='object'||Array.isArray(value)||typeof value.libraryId!=='string')return null;
      for(const key of ['title','fileName','excerpt','sourceVersion','relativePath'])if(value[key]!=null&&typeof value[key]!=='string')return null;
      let location=null;const raw=value.location;
      if(raw!=null){if(typeof raw!=='object'||!Number.isSafeInteger(raw.startLine)||!Number.isSafeInteger(raw.endLine)||raw.startLine<1||raw.endLine<raw.startLine)return null;
        const pdf=raw.pageSource==='page-map'&&Number.isSafeInteger(raw.pdfPage)&&raw.pdfPage>0;
        location={coordinateSpace:'extracted-text',kind:pdf?'pdf-page':'text-line',startLine:raw.startLine,endLine:raw.endLine,
          pageHint:Number.isSafeInteger(raw.pageHint)&&raw.pageHint>0?raw.pageHint:null,pdfPage:pdf?raw.pdfPage:null,pdfPageEnd:pdf&&Number.isSafeInteger(raw.pdfPageEnd)&&raw.pdfPageEnd>=raw.pdfPage?raw.pdfPageEnd:pdf?raw.pdfPage:null,
          pageSource:['page-map','map-gap','map-unusable','unmapped','not-pdf'].includes(raw.pageSource)?raw.pageSource:'not-pdf'};
        for(const name of ['startChar','endChar','startOffset','endOffset'])if(raw[name]!==undefined){if(!Number.isSafeInteger(raw[name])||raw[name]<0)return null;location[name]=raw[name];}if(location.startChar!==undefined&&location.endChar!==undefined&&location.endChar<location.startChar)return null;
      }
      const quality=normalizeParseQuality(value.parseQuality);
      const status=value.status&&typeof value.status==='object'?value.status:{};
      return {libraryId:value.libraryId,title:value.title??null,fileName:value.fileName??null,relativePath:value.relativePath,
        excerpt:value.excerpt??null,sourceVersion:value.sourceVersion??null,citationId:/^ksrc-[a-f0-9]{16}$/.test(value.citationId||'')?value.citationId:null,
        citationOrigin:['engine','engine-fragment','source-window','read','catalogue'].includes(value.citationOrigin)?value.citationOrigin:null,
        location,status:{identity:['verified','partial','conflict','undetermined','not-assessed'].includes(status.identity)?status.identity:'not-assessed',identityDetail:typeof status.identityDetail==='string'?status.identityDetail:null,locationVerified:!!location&&status.locationVerified===true,support:'not-assessed'},
        truncated:value.truncated===true,truncationNotices:safeStrings(value.truncationNotices),parseQuality:quality.value,parseQualityDegraded:quality.degraded||value.parseQualityDegraded===true,
        structureType:typeof value.structureType==='string'?value.structureType:'unknown',contentWarnings:safeStrings(value.contentWarnings),matched:safeStrings(value.matched),mismatched:safeStrings(value.mismatched),documentId:typeof value.documentId==='string'?value.documentId:undefined,conflicts:Array.isArray(value.conflicts)?value.conflicts.filter(v=>v&&typeof v==='object').slice(0,20).map(v=>({kind:typeof v.kind==='string'?v.kind:'unknown',detail:typeof v.detail==='string'?v.detail:null})):[]};
    }

    function createPanelStore() {
      const snapshots = new Map(), listeners = new Set();
      const empty = Object.freeze({ kind: 'empty' });
      return {
        snapshot: sessionId => snapshots.get(sessionId) || empty,
        subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
        select(sessionId, value) {
          if (typeof sessionId !== 'string' || !sessionId) return false;
          snapshots.delete(sessionId); snapshots.set(sessionId, Object.freeze(value));
          while (snapshots.size > 100) snapshots.delete(snapshots.keys().next().value);
          for (const listener of [...listeners]) try{listener();}catch{panelDiagnostic('PANEL_LISTENER_FAILED','store');}
          return true;
        },
        clear() { snapshots.clear(); for (const listener of [...listeners]) try{listener();}catch{panelDiagnostic('PANEL_LISTENER_FAILED','store');} listeners.clear(); },
      };
    }
    function panelOpener(props, value) {
      const runtime = panelRuntime;
      if (!runtime || typeof props.sessionId !== 'string') return null;
      // A callback from a hidden/old conversation cannot navigate the newly selected conversation.
      return () => {
        if (panelRuntime !== runtime || runtime.sidebar.mounted.getSnapshot() !== props.sessionId) return;
        runtime.store.select(props.sessionId, value);
        runtime.sidebar.openTab(PANEL_KIND);
      };
    }
    // Build from the host's observable journal, never from whether a tool card was expanded.
    function journalTree(window,sessionId='') {
      if(!window||typeof window!=='object'||!Array.isArray(window.entries))throw Error('PANEL_SNAPSHOT_INVALID');
      const calls=new Map(),groups=new Map(),failures=[],answers=new Map();let budget=null,jobUsage=null,invalidRecords=0,qualityDegraded=0,sourceRejected=0,recordFailures=0,issued=new Map(),turn='unobserved',questionTitle='';
      for(const row of window.entries){
        try{
          const e=row?.event||row;if(!e||typeof e!=='object'){invalidRecords++;recordFailures++;continue;}const d=e.data&&typeof e.data==='object'?e.data:{};
          if(e.type==='turn/start'){budget=null;issued=new Map();turn=String(e.seq??d.turn??'unobserved');}
          if(e.type==='user/message'&&d.source?.kind==='user'&&Array.isArray(d.content))questionTitle=d.content.filter(b=>b?.type==='text'&&typeof b.text==='string').map(b=>b.text).join(' ').replace(/\s+/g,' ').slice(0,48);
          if(e.type==='tool/call'&&TOOL_NAMES.includes(d.name)&&typeof d.callId==='string')calls.set(d.callId,d.name);
          if(e.type==='assistant/message'){
            const message=d.message;if(!message||typeof message.id!=='string'||!Array.isArray(message.content))continue;
            const prose=message.content.filter(b=>b?.type==='text'&&typeof b.text==='string').map(b=>b.text).join('\n');if(!prose)continue;
            const references=[...new Set([...prose.matchAll(/\[(\d{1,3})\]/g)].map(m=>Number(m[1])))];
            answers.set(message.id,{assistantMessageId:message.id,sessionId,turn,prose,questionTitle,
              citations:references.map(number=>{const binding=issued.get(number);return binding?{number,...binding}:{number,unmatched:true,reason:window.hasMore?'HISTORY_NOT_LOADED':'NUMBER_NOT_ISSUED'};})});continue;
          }
          if(e.type!=='tool/result')continue;const message=d.message||{},id=message.toolCallId;if(!calls.has(id))continue;
          const name=calls.get(id);calls.delete(id);const block={...message,kind:'tool-result',call:{name},callId:id,meta:d.meta,error:d.error};
          const failure=failureOf(block);if(failure){failures.push(failure);if(failure.budget&&(!budget||failure.budget.used>=budget.used))budget=failure.budget;}
          let result;try{result=JSON.parse(toolText(block));if(name==='knowledge_search'){const rows=Array.isArray(result.usage)?result.usage:[];jobUsage={knownTokens:rows.reduce((n,r)=>n+(Number.isSafeInteger(r?.total_tokens)&&r.total_tokens>=0?r.total_tokens:0),0),unknown:!rows.length||rows.some(r=>!Number.isSafeInteger(r?.total_tokens)||r.total_tokens<0)};}if(safeBudget(result.budget)&&(!budget||result.budget.used>=budget.used))budget=result.budget;}catch{}
          const payload=readPayload(block),cards=[];sourceRejected+=payload?.invalidSources||0;invalidRecords+=payload?.invalidSources||0;
          for(const raw of payload?.sources||[]){const card=sourceCard(raw);if(!card){invalidRecords++;sourceRejected++;continue;}if(card.parseQualityDegraded)qualityDegraded++;cards.push(card);
            const lib=card.libraryId||'未标注知识库';if(!groups.has(lib))groups.set(lib,new Map());const docs=groups.get(lib);
            const key=JSON.stringify([card.relativePath||card.fileName||card.title,card.sourceVersion]);if(!docs.has(key))docs.set(key,{key,title:card.title||card.fileName||'未命名来源',cards:[],seen:new Set()});
            const doc=docs.get(key),fragment=JSON.stringify([card.location,card.excerpt,card.citationOrigin]);if(!doc.seen.has(fragment)){doc.seen.add(fragment);doc.cards.push(card);}
          }
          const mapping=payload?.answerCitations||result?.answerCitations;
          if(mapping?.version===1&&Array.isArray(mapping.entries))for(const entry of mapping.entries){
            if(!Number.isSafeInteger(entry?.number)||entry.number<1||entry.number>300)continue;
            let reason=mapping.sessionId!==sessionId?'SESSION_MISMATCH':turn==='unobserved'?'HISTORY_NOT_LOADED':mapping.turnKey!==sessionId+'#'+turn?'TURN_MISMATCH':null;
            const sameId=cards.filter(card=>card.citationId===entry.citationId);
            const matches=sameId.filter(card=>card.sourceVersion===entry.sourceVersion&&typeof card.excerpt==='string'&&card.excerpt.trim()&&card.location&&card.citationOrigin!=='catalogue');
            if(!reason&&matches.length!==1)reason=(payload?.rejectedSources||[]).some(c=>c.citationId===entry.citationId)?'SOURCE_FIELDS_INVALID':sameId.length?'VERSION_MISMATCH':window.hasMore?'HISTORY_NOT_LOADED':'SOURCE_MISSING';
            const previous=issued.get(entry.number);if(previous&&(previous.citationId!==entry.citationId||previous.sourceVersion!==entry.sourceVersion||previous.reason==='NUMBER_CONFLICT'))reason='NUMBER_CONFLICT';
            issued.set(entry.number,reason?{citationId:entry.citationId,sourceVersion:entry.sourceVersion,unmatched:true,reason}:{citationId:entry.citationId,sourceVersion:entry.sourceVersion,card:matches[0]});
          }
        }catch{invalidRecords++;recordFailures++;}
      }
      if(qualityDegraded)panelDiagnostic('PARSE_QUALITY_INVALID','quality');
      return {kind:'tree',groups:[...groups].map(([library,docs])=>({library,documents:[...docs.values()]})),failures:failures.slice(-20),pending:calls.size,budget,jobUsage,hasMore:window.hasMore===true,invalidRecords,qualityDegraded,sourceRejected,recordFailures,answers:[...answers.values()]};
    }
    // A subscription baseline is history, never a reason to navigate. Every turn is
    // consumed on its first fresh knowledge call, including when focus forbids opening.
    function createAutoOpenGate(runtime,id,initial){
      const rows=snapshot=>(snapshot?.entries||[]).map(row=>row?.event||row);
      let water=Math.max(-1,...rows(initial).map(e=>Number.isSafeInteger(e?.seq)?e.seq:-1));
      let turn=rows(initial).filter(e=>e?.type==='turn/start').at(-1)?.seq;
      if(Number.isSafeInteger(turn))runtime.currentTurn={id,key:id+'#'+turn};
      runtime.autoTurns??=new Map(); runtime.closedTurns??=new Set();
      return snapshot=>{
        const fresh=rows(snapshot).filter(e=>Number.isSafeInteger(e?.seq)&&e.seq>water).sort((a,b)=>a.seq-b.seq);
        for(const e of fresh){water=e.seq;if(e.type==='turn/start'){turn=e.seq;runtime.currentTurn={id,key:id+'#'+turn};}
          if(e.type!=='tool/call'||!TOOL_NAMES.includes(e.data?.name)||!Number.isSafeInteger(turn))continue;
          const key=id+'#'+turn;if(runtime.autoTurns.has(key))continue;
          runtime.autoTurns.set(key,true);while(runtime.autoTurns.size>500){const old=runtime.autoTurns.keys().next().value;runtime.autoTurns.delete(old);runtime.closedTurns.delete(old);}
          runtime.currentTurn={id,key};
          if(runtime.autoOpenMode!=='first-call'||runtime.closedTurns.has(key)||runtime.sidebar.mounted.getSnapshot()!==id)continue;
          // Missing host observability is conservative: manual opening still works.
          if(typeof runtime.sidebar.active!=='function'||typeof runtime.sidebar.isExpanded!=='function')continue;
          const active=runtime.sidebar.active();if(runtime.sidebar.isExpanded()&&active?.kind!==PANEL_KIND)continue;
          runtime.sidebar.openTab(PANEL_KIND);
        }
      };
    }
    function followSession(runtime,sessions){
      let release=()=>{},disposed=false,generation=0;const retries=new Map();
      const mount=()=>{
        const id=runtime.sidebar.mounted.getSnapshot(),own=++generation;release();release=()=>{};if(!id||disposed)return;
        const last=runtime.trees.snapshot(id);runtime.trees.select(id,{...last,kind:'loading'});
        let ref;try{ref=sessions.retain(id,{source:'knowledge-panel'});}catch{runtime.trees.select(id,{...last,kind:'unavailable',error:panelDiagnostic('PANEL_RETAIN_FAILED','retain')});return;}
        let unsubscribe=()=>{};release=()=>{unsubscribe();ref.release();};
        ref.ready.then(binding=>{
          if(disposed||own!==generation)return;
          fetch('/knowledge-panel?sessionId='+encodeURIComponent(id),{credentials:'same-origin'}).then(r=>r.ok?r.json():null).then(v=>{if(!disposed&&own===generation&&v)runtime.autoOpenMode=v.autoOpenMode==='first-call'?'first-call':'manual';}).catch(()=>{});
          const first=binding.eventSource.getSnapshot();
          const gate=createAutoOpenGate(runtime,id,first);
          const publish=()=>{if(disposed||own!==generation)return;try{const snapshot=binding.eventSource.getSnapshot();runtime.trees.select(id,journalTree(snapshot,id));gate(snapshot);}
            catch{runtime.trees.select(id,{...runtime.trees.snapshot(id),updateFailed:true,error:panelDiagnostic('PANEL_UPDATE_FAILED','subscription')});}};
          try{unsubscribe=binding.eventSource.subscribe(publish);publish();}catch{runtime.trees.select(id,{...last,updateFailed:true,error:panelDiagnostic('PANEL_SUBSCRIBE_FAILED','subscription')});}
        }).catch(()=>{if(!disposed&&own===generation)runtime.trees.select(id,{...last,kind:'unavailable',error:panelDiagnostic('PANEL_READY_FAILED','ready')});});
      };
      runtime.reload=id=>{if(disposed||runtime.sidebar.mounted.getSnapshot()!==id)return false;const count=retries.get(id)||0;if(count>=3)return false;retries.set(id,count+1);mount();return true;};
      const off=runtime.sidebar.mounted.subscribe(mount);mount();return()=>{disposed=true;generation++;off();release();runtime.reload=null;};
    }
    const SERVICE_LABELS = { ready: '服务已启动 · 隔离已启用', unreachable: '服务不可达（可能未启动或已关闭）',
      unauthorized: '服务可达，但认证失败', 'not-isolated': '服务可达，但隔离未就绪', 'build-mismatch': '组件版本不一致，请完成更新并刷新页面', unhealthy: '服务健康检查失败', unknown: '服务状态未知' };
    const LIBRARY_ERRORS={FOLDER_OVERLAPS_STORAGE:'所选文件夹包含插件数据目录，请选择独立的资料子文件夹。',LIBRARY_CAPACITY_EXCEEDED:'文件数超过建库上限；未提高限制或发布部分库。',PREVIEW_LIMIT:'预检数量已达上限，请关闭其他预检或稍后重试。',CONFIG_INVALID:'库配置无法读取，请检查配置后再试。',NAME_INVALID:'名称无效，请使用不超过 120 字的名称。',INVALID_LIBRARY_ACTION:'操作参数无效，请重新打开向导。',LOCAL_BUILD_FAILED:'本地构建未完成，旧配置未被替换。',LIBRARY_ACTION_FAILED:'操作未完成；请检查预检结果及服务状态。',FOLDER_INVALID:'请选择有效的文件夹。',FOLDER_OUTSIDE_WORKSPACE:'文件夹须在当前工作区内。',
      LINK_NOT_ALLOWED:'不支持链接或联接目录，请选原目录。',WORKSPACE_TOO_BROAD:'请先打开具体工作区，不使用整盘作为范围。',
      CONFIG_CHANGED:'配置已变化，请重新预检。',SOURCE_CHANGED:'原件已变化，请重新预检。',PREFLIGHT_FAILED:'预检未通过，请处理异常文件后再试。',
      UPDATE_UNAVAILABLE:'此库尚未使用向导建立，请沿用原注册脚本更新。',UPDATE_WORKSPACE_MISMATCH:'请回到建立此库时的工作区更新。',
      PREVIEW_EXPIRED:'预检已过期，请重新预检。',PREVIEW_FOREIGN:'此预检不属于当前会话。',CONSENT_REQUIRED:'请先确认目录与数据使用授权。',
      PUBLISH_BUSY:'另一个发布操作尚未完成，请稍后检查列表。',LIBRARY_NOT_VISIBLE:'此库在当前工作区不可用，未自动选择其他库。',
      SELECTION_NOT_SAVED:'选择未保存，旧选择保持不变。',SELECTION_FILE_UNREADABLE:'选择配置无法读取，未覆盖原文件。',
      BUILD_MISMATCH:'组件版本不一致，请完成更新并刷新页面。',SERVICE_UNAVAILABLE:'知识库服务不可用，请检查服务状态。',SESSION_UNAVAILABLE:'会话尚未就绪，请稍后重试。'};
    function LibraryControls(props) {
      const [wizard,setWizard]=React.useState(false),[folder,setFolder]=React.useState(''),[name,setName]=React.useState(''),
        [preview,setPreview]=React.useState(null),[consent,setConsent]=React.useState(false),[working,setWorking]=React.useState(false),
        [notice,setNotice]=React.useState(''),[remove,setRemove]=React.useState(false),[errorCode,setErrorCode]=React.useState('');
      const epoch=React.useRef(0), opener=React.useRef(null);
      React.useEffect(()=>{epoch.current++;setWizard(false);setPreview(null);setConsent(false);setFolder('');setName('');setWorking(false);setNotice('');setErrorCode('');setRemove(false);return()=>{epoch.current++;};},[props.sessionId]);
      const action=async value=>{
        const own=epoch.current;setWorking(true);setErrorCode('');setNotice('正在处理…');
        try {
          const r=await fetch('/knowledge-panel?sessionId='+encodeURIComponent(props.sessionId),{method:'POST',credentials:'same-origin',
            headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
          const v=await r.json();if(own!==epoch.current)return null;
          if(!r.ok){const code=Object.hasOwn(LIBRARY_ERRORS,v.code)?v.code:'LIBRARY_ACTION_FAILED';setErrorCode(code);setNotice(LIBRARY_ERRORS[code]||'操作未完成；请检查预检结果及服务状态。');return null;}
          if(Array.isArray(v.libraries))props.onProjection(v);
          setNotice(v.code==='WORKSPACE_DEFAULT_CHANGED'?(v.selectionPersisted===false?'默认仅在本次宿主运行中保留，未写入磁盘。':'默认已保存，仅影响之后新建的会话；当前会话不变。'):v.code==='SESSION_LIBRARY_CHANGED'?'已切换，仅影响后续调用；在途查询和历史来源保持原库。':'');
          return v;
        } catch {if(own===epoch.current)setNotice('无法联系建库接口；未确认发布，请检查服务状态后刷新列表。');return null;}
        finally{if(own===epoch.current)setWorking(false);}
      };
      const close=async()=>{
        if(working)return;
        const own=epoch.current;
        if(preview)await action({libraryAction:'cancel',previewId:preview.previewId});
        if(own!==epoch.current)return;
        setWizard(false);setPreview(null);setConsent(false);opener.current?.focus();
      };
      const begin=()=>{setWizard(true);setPreview(null);setConsent(false);setNotice('');};
      const selected=props.state?.selectedLibraryId || '';
      const available=Array.isArray(props.state?.libraries)?props.state.libraries:[];
      return h('section',{className:'kw-library'},
        h('label',null,'当前会话知识库',h('select',{'aria-label':'当前会话知识库',value:selected,disabled:working||!props.state||props.state.libraryControlUnavailable,
          onChange:e=>action({libraryAction:'select',libraryId:e.target.value||null})},
          h('option',{value:''},'未限定 · 按工作区可见范围'),
          selected&&!available.some(v=>v.id===selected)?h('option',{value:selected},'所选库不可用，请检查工作区或配置'):null,
          ...available.map(v=>h('option',{key:v.id,value:v.id},v.name+' · '+(v.documents===null?'数量未知':v.documents+' 篇')+(v.state==='ready'?'':' · 待检查'))))),
        props.state?.selectionUnavailable?h('p',{role:'alert'},'所选库目前不可见，未自动选择其他库。'):null,
        props.state?.selectionPersisted===false?h('p',null,'选择仅在本次宿主运行中保留；配置 tokenFile 后才能持久化。'):null,
        h('small',null,'工作区默认：'+(available.find(v=>v.id===props.state?.workspaceDefault)?.name || (props.state?.workspaceDefault?'当前不可用':'未设置'))),
        Array.isArray(props.state?.libraries)&&!available.length&&!props.state?.libraryControlUnavailable?h('p',null,'尚无可见知识库。添加文件夹不会改动原件；选择工作区并不等于已建库。'):null,
        h('div',null,h('button',{type:'button',ref:opener,disabled:working||!Array.isArray(props.state?.libraries)||props.state?.libraryControlUnavailable,onClick:begin},'添加知识库'),
          h('button',{type:'button',disabled:working||!selected,onClick:()=>action({libraryAction:'default',libraryId:selected})},'设为工作区默认'),
          props.state?.workspaceDefault?h('button',{type:'button',disabled:working,onClick:()=>action({libraryAction:'default',libraryId:null})},'清除默认'):null,
          h('button',{type:'button',disabled:working||!available.find(v=>v.id===selected)?.updateAvailable,onClick:async()=>{const v=await action({libraryAction:'preview',libraryId:selected});if(v){setWizard(true);setPreview(v);setConsent(false);}}},'检查更新'),h('button',{type:'button',disabled:working||!selected,onClick:()=>setRemove(true)},'移除登记')),
        remove?h('div',{role:'alert'},'只注销本库，保留原件与衍生版本；其他会话的所选库将显示不可用。',h('button',{type:'button',disabled:working,onClick:async()=>{const v=await action({libraryAction:'remove',libraryId:selected,consent:true});if(v)setRemove(false);}},'确认注销'),h('button',{type:'button',disabled:working,onClick:()=>setRemove(false)},'取消')):null,
        props.state?.libraryControlUnavailable?h('p',null,'建库与切库接口不可用；可继续用 /knowledge-use，或检查服务及宿主版本。'):null,
        wizard?h('div',{role:'dialog','aria-label':'添加或更新知识库',className:'kw-settings',onKeyDown:e=>{if(e.key==='Escape')close();}},
          h('div',{className:'kw-heading'},h('strong',null,preview?'确认本地预检':'添加知识库'),h('button',{type:'button','aria-label':'关闭建库向导',disabled:working,onClick:close},'×')),
          !preview?[h('label',{key:'folder'},'文件夹路径',h('input',{type:'text',maxLength:512,value:folder,'aria-label':'知识库文件夹路径',autoFocus:true,onChange:e=>setFolder(e.target.value)})),
            h('label',{key:'name'},'知识库名称',h('input',{type:'text',maxLength:120,value:name,'aria-label':'知识库名称',onChange:e=>setName(e.target.value)})),
            h('p',{key:'scope'},'可见工作区：'+(props.state?.workspace||'未确认')+'。文件夹须在此工作区内；不扩大到整盘。'),
            h('p',{key:'privacy'},'本地预检不调用模型、不外发、不写原件。支持 Markdown 与文本 PDF；图片与扫描件没有 OCR。'),
            h('button',{key:'preview',type:'button',className:'kw-primary',disabled:working||!folder.trim(),onClick:async()=>{const v=await action({libraryAction:'preview',root:folder.trim(),name:name.trim()||undefined});if(v)setPreview(v);}},working?'正在本地预检…':'本地预检')]:
          h('div',null,h('p',null,preview.name+' · '+preview.documents+' 篇可读取；Markdown '+preview.markdown+'，PDF '+preview.pdf+'；文本 '+preview.totalBytes+' 字节'),
            h('p',null,'可见工作区：'+preview.workspace),h('p',null,'原件目录：'+preview.root),
            h('p',null,'差异：新增 '+preview.diff.added.length+'，修改 '+preview.diff.changed.length+'，删除 '+preview.diff.removed.length),
            h('details',null,h('summary',null,'排除项、异常与 PDF 限制'),
              ...[...preview.problems,...preview.excluded,...preview.quality].slice(0,40).map((v,i)=>h('p',{key:i},(v.path||'')+' · '+(v.reason||v.flags?.join(', ')||'')+(v.limit?' · 上限 '+v.limit:'')+(v.limitations?' · 扫描文字未支持，公式、表格与复杂行序须回原件核对':'')))),
            !preview.canPublish?h('p',{role:'alert'},'预检未通过，不能发布；不会跳过坏文件、提高上限或覆盖旧库。'):null,
            h('p',null,'确认后允许此库相关片段发送给引擎提供方 '+(props.state?.engineProvider||'未配置')+'，读取内容进入当前 DSH 对话模型上下文。预检和发布本身仍不外发。'),
            h('label',null,h('input',{type:'checkbox',checked:consent,disabled:working,onChange:e=>setConsent(e.target.checked)}),'我确认目录、可见范围与上述数据使用授权（包含预检所示新增/修改文件）'),
            h('button',{type:'button',className:'kw-primary',disabled:working||!consent||!preview.canPublish,onClick:async()=>{const v=await action({libraryAction:'publish',previewId:preview.previewId,consent:true});if(v){setWizard(false);setPreview(null);setConsent(false);const listed=await action({libraryAction:'refresh'});if(listed)setNotice('知识库已建立。请从顶部下拉选择用于本会话；设默认仅影响新会话。');}}},working?'正在建立…':'建立并校验知识库')),
          h('button',{type:'button',disabled:working,onClick:close},'取消')):null,
        h('p',{role:'status'},notice),errorCode?h('details',null,h('summary',null,'技术详情'),h('code',null,errorCode)):null);
    }
    function PanelControls(props) {
      const [state, setState] = React.useState(null), [limit, setLimit] = React.useState(''), [notice, setNotice] = React.useState('');
      const [busy,setBusy]=React.useState(false), [confirmStop,setConfirmStop]=React.useState(false), [settingsOpen,setSettingsOpen]=React.useState(false);
      const settingsButton=React.useRef(null);
      const generation=React.useRef(0);
      React.useEffect(()=>{generation.current++;return ()=>{generation.current++;};},[props.sessionId]);
      React.useEffect(() => {
        if (props.active === false) return;
        let alive = true, timer, controller;
        const refresh = async () => {
          controller = new AbortController();
          try {
            const response = await fetch('/knowledge-panel?sessionId=' + encodeURIComponent(props.sessionId), { credentials: 'same-origin', signal: controller.signal });
            if (!response.ok) throw new Error();
            const value = await response.json(); if (alive) {setState(value);if(panelRuntime)panelRuntime.autoOpenMode=value.autoOpenMode==='first-call'?'first-call':'manual';}
          } catch { if (alive) setState({ service: 'unknown', unavailable: true }); }
          if (alive) timer = setTimeout(refresh, 10000);
        };
        setState(null); setLimit(''); setNotice(''); refresh();
        return () => { alive = false; clearTimeout(timer); controller?.abort(); };
      }, [props.sessionId, props.active]);
      const save = async () => {
        const value = Number(limit || state?.maxCalls);
        if (!Number.isInteger(value) || value < 1 || value > 30) { setNotice('请输入 1–30 的整数。'); return; }
        const own=generation.current; setNotice('正在保存…');
        try {
          const response = await fetch('/knowledge-panel?sessionId=' + encodeURIComponent(props.sessionId), { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ maxCalls: value }) });
          if (!response.ok) throw new Error();
          const saved = await response.json(); if(own!==generation.current)return; setState(previous => ({ ...previous, ...saved })); setLimit('');
          setNotice('已保存：仅本会话，下一轮生效。当前轮计数不重置。');
        } catch { if(own!==generation.current)return; setNotice('未保存。请确认宿主已加载新插件后重试。'); }
      };
      const action = async kind => {
        if(busy || props.active===false) return;
        const own=generation.current;setBusy(true);setConfirmStop(false);
        setNotice(kind==='start'?'正在启动服务…':kind==='stop'?'正在停止服务…':'正在重新检查…');
        try {
          const response=await fetch('/knowledge-panel?sessionId='+encodeURIComponent(props.sessionId),{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:kind})});
          if(!response.ok) throw Error();const value=await response.json();
          if(own!==generation.current)return;
          setState(previous=>({...previous,...value}));
          setNotice(value.code==='ACTION_FAILED'?'操作失败 ACTION_FAILED；请使用备用命令检查。':value.code==='CONTROL_UNAVAILABLE'?'当前不是可受控安装实例。':value.code==='ACTION_BUSY'?'另一项操作正在进行。':value.service==='ready'?'服务可用，隔离已核验。':kind==='stop'?'停止操作已结束；以健康状态和端口核验为准。':'健康状态已更新；不可达不等于进程已停止。');
        } catch {if(own===generation.current)setNotice('控制接口不可用；未确认操作完成。');}
        finally{if(own===generation.current)setBusy(false);}
      };
      /**
       * Ask **this session's** query to stop.
       *
       * It is a separate action from the service stop on purpose: this one cancels the query this session
       * started and nothing else, while `stop` shuts the shared backend down for every session and keeps its
       * confirmation step. The request itself does no work in the service beyond aborting the local call; the
       * plugin then asks the service to end the job and reports whether that was confirmed.
       */
      const cancelQuery = async (requestId = null) => {
        if (busy || props.active === false) return;
        const own = generation.current; setBusy(true);
        setNotice('正在取消本次查询…');
        try {
          const response = await fetch('/knowledge-panel?sessionId=' + encodeURIComponent(props.sessionId),
            { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ cancelQuery: true, ...(requestId ? {requestId} : {}) }) });
          if (!response.ok) throw Error();
          const value = await response.json();
          if (own !== generation.current) return;
          setState(previous => ({ ...previous, query: value.query, queries: value.queries }));
          setNotice(value.code === 'NO_ACTIVE_QUERY'
            ? '当前未跟踪到本会话作业；历史未收口状态未知。'
            : '已请求取消；若下方仍显示在途或“取消未确认”，说明作业可能仍在结束过程中。');
        } catch { if (own === generation.current) setNotice('取消失败：未能联系插件接口，未确认本次查询已停止。'); }
        finally { if (own === generation.current) setBusy(false); }
      };
      const commands = state?.commands;
      // The second axis. "The service is up" and "a query can be admitted right now" are different facts, and
      // a panel that showed only the first is what let a healthy 200 read as "search will work" while every
      // submission was being refused. The live view is this session's own job: a phase, a mode and a clock.
      const query = state?.query || null;
      const QUERY_PHASES = { submitting: '正在提交', accepted: '已接纳', polling: '正在检索', cancelling: '正在停止并清理',
        completed: '已完成', failed: '失败', cancelled: '已取消', unknown: '状态未知' };
      const serviceBusy = state?.serviceQuery?.busy === true;
      return h('header', { className: 'kw-controls' },
        h('div',{className:'kw-heading'},h('h3',null,'知识库'),h('button',{type:'button',ref:settingsButton,'aria-label':'知识库设置',onClick:()=>setSettingsOpen(true)},'⚙')),
        h('strong', { role: 'status' }, state ? (SERVICE_LABELS[state.service] || SERVICE_LABELS.unknown) : '正在检查服务…'),

        ...(Array.isArray(state?.queries) ? state.queries : (query ? [query] : [])).filter(record=>record===query || record.requestId===query?.requestId || record.cancellable).map(record =>
          h('div', {key: record.requestId || record.phase},
            h('p', {role:'status'}, (QUERY_PHASES[record.phase] || '状态未知') + ' · ' + record.elapsedSeconds + ' 秒'),
            h('small',null,record.mode),
            h('details',null,h('summary',null,'查询详情'),h('small',null,'服务状态 '+(record.jobState || '未知')+' · 请求 ID '+(record.requestId || '未知'))),
            record.cancellable ? h('button',{type:'button',style:S.button,disabled:busy || props.active===false || record.phase==='cancelling',
              onClick:()=>cancelQuery(record.requestId)}, record.live ? '取消本次查询' : '继续取消') : null,
            record.cancellationConfirmed === false && !['completed','failed','cancelled'].includes(record.phase) ? h('p',{role:'alert'},'尚未确认停止：作业可能仍在收口，请检查状态或继续取消。') : null)),
        !query&&serviceBusy ? h('p',{role:'status'},'服务正在处理其他查询；归属未确认，不能取消别人的任务。') : null,
        state?.unavailable ? h('p', null, '状态接口不可用：请重启 DSH 加载新插件；不能据此判断后端已关闭。') : null,
        state && state.service!=='ready' ? h('button',{type:'button',className:'kw-primary',disabled:busy || !state?.controlAvailable,onClick:()=>action('start')},'启动服务') : null,

        confirmStop ? h('div',{role:'alert'},'停止共享后端会影响所有知识库会话；在途查询会被取消，未完成模型用量可能未知。', h('button',{type:'button',disabled:busy,onClick:()=>action('stop')},'确认停止共享后端'),h('button',{type:'button',onClick:()=>setConfirmStop(false)},'取消')):null,
        h(LibraryControls,{sessionId:props.sessionId,state,onProjection:value=>setState(previous=>({...previous,...value}))}),
        h('button',{type:'button',className:'kw-budget',onClick:()=>setSettingsOpen(true)},props.budget ? '工具调用 '+props.budget.used+' / '+props.budget.max : '工具调用 — / '+(state?.maxCalls || 6)),
        settingsOpen ? h('div',{role:'dialog','aria-modal':false,'aria-label':'知识库设置',className:'kw-settings',onKeyDown:e=>{if(e.key==='Escape'){setSettingsOpen(false);settingsButton.current?.focus();}}},
          h('div',{className:'kw-heading'},h('strong',null,'知识库设置'),h('button',{type:'button','aria-label':'关闭设置',onClick:()=>{setSettingsOpen(false);settingsButton.current?.focus();}},'×')),
          h('details',{className:'kw-menu'},h('summary',{'aria-label':'服务管理'},'服务管理 ⋯'),h('button',{type:'button',disabled:busy,onClick:()=>action('status')},'检查状态'),h('button',{type:'button',disabled:busy || !state?.controlAvailable,onClick:()=>{setSettingsOpen(false);setConfirmStop(true);}},'停止共享服务')),
          h('label',null,'每轮知识工具调用上限（本会话）'),
          h('div',{className:'kw-stepper'},h('button',{type:'button','aria-label':'减少调用上限',onClick:()=>setLimit(String(Math.max(1,Number(limit || state?.maxCalls || 6)-1)))},'−'),
            h('input',{type:'number',min:1,max:30,step:1,value:limit || state?.maxCalls || 6,'aria-label':'每轮知识工具调用上限',autoFocus:true,onChange:e=>setLimit(e.target.value)}),
            h('button',{type:'button','aria-label':'增加调用上限',onClick:()=>setLimit(String(Math.min(30,Number(limit || state?.maxCalls || 6)+1)))},'+')),
          h('p',null,'搜索、定位、列库和读取合计；失败尝试也计数，不是模型调用额度。仅本会话，下一轮生效，当前轮保持原上限。'),
          h('label',null,'自动展开',h('select',{'aria-label':'自动展开知识库',value:state?.autoOpenMode||'manual',disabled:!state||state.settingsReadable===false,onChange:async e=>{const mode=e.target.value;try{const r=await fetch('/knowledge-panel?sessionId='+encodeURIComponent(props.sessionId),{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({autoOpenMode:mode})});if(!r.ok)throw Error();if(panelRuntime)panelRuntime.autoOpenMode=mode;setState(v=>({...v,autoOpenMode:mode}));setNotice('已保存：本实例所有会话的展开偏好，不改变工具额度。');}catch{setNotice('展开偏好未保存。');}}},h('option',{value:'first-call'},'本轮首次知识调用'),h('option',{value:'manual'},'仅手动打开'))),
          h('button',{type:'button',onClick:()=>setLimit('6')},'恢复默认'),
          h('button',{type:'button',className:'kw-primary',disabled:!state || state.unavailable || state.settingsReadable===false,onClick:save},'保存上限'),
          state?.settingsReadable===false ? h('p',null,'设置文件无法读取，未覆盖原文件。') : null,
          state?.persisted===false ? h('p',null,'仅在本次宿主运行期间保留。') : null,
        h('details', null, h('summary', null, '备用启动 / 关闭命令'),
          h('p', null, '在 PowerShell 执行下面命令；状态刷新为“服务已启动”后再提问。关闭会影响使用该后端的所有会话。'),
          commands ? [h('div', { key: 'start' }, '启动后端（不启动另一个 DSH）：', h('pre', { style: S.pre }, commands.start)),
            h('div', { key: 'stop' }, '关闭后端：', h('pre', { style: S.pre }, commands.stop))]
            : h('p', null, '未识别安装实例，无法安全生成启动命令。请使用该实例的 trial/start.mjs --service-only 和 service-task.mjs stop。')),
        ) : null,
        h('p', { role: 'status' }, notice));
    }
    const EMPTY_STORE=createPanelStore();
    function reloadPanel(sessionId){return panelRuntime?.reload?.(sessionId)===true;}
    const PanelErrorBoundary=typeof React.Component==='function'?class extends React.Component{
      constructor(props){super(props);this.state={error:null,attempts:0};}
      static getDerivedStateFromError(){return {error:true};}
      componentDidCatch(){this.setState({error:panelDiagnostic('PANEL_RENDER_FAILED','render')});}
      render(){if(this.state.error)return h('section',{role:'alert'},h('p',null,'面板渲染失败，已有来源未删除。'),h('small',null,this.state.error.correlationId||''),h('button',{type:'button',disabled:this.state.attempts>=3,onClick:()=>{reloadPanel(this.props.sessionId);this.setState({error:null,attempts:this.state.attempts+1});}},'重新加载面板'));return this.props.children;}
    }:props=>props.children;
    function SourcePanelBody(props){
      const runtime=React.useSyncExternalStore(subscribeRuntime,()=>panelRuntime,()=>null);
      const store=runtime?.store||EMPTY_STORE,trees=runtime?.trees||EMPTY_STORE;
      const value=React.useSyncExternalStore(store.subscribe,()=>store.snapshot(props.sessionId),()=>store.snapshot(props.sessionId));
      const active=React.useSyncExternalStore(runtime?.sidebar.mounted.subscribe||EMPTY_STORE.subscribe,()=>runtime?.sidebar.mounted.getSnapshot()===props.sessionId,()=>false);
      const tree=React.useSyncExternalStore(trees.subscribe,()=>trees.snapshot(props.sessionId),()=>trees.snapshot(props.sessionId));
      if(!runtime)return h('p',{role:'status'},'知识库面板不可用。');
      return h(Workbench,{key:props.sessionId,sessionId:props.sessionId,active,runtime,tree,value});
    }
    function SourcePanel(props){return typeof React.Component==='function'?h(PanelErrorBoundary,{key:props.sessionId,sessionId:props.sessionId},h(SourcePanelBody,props)):h(SourcePanelBody,props);}
    function readableLocation(card){const l=card.location;if(!l)return '尚未读取正文，无摘录位置';return l.pageSource==='page-map'&&l.pdfPage?'PDF 第 '+l.pdfPage+(l.pdfPageEnd&&l.pdfPageEnd!==l.pdfPage?'–'+l.pdfPageEnd:'')+' 页 · L'+l.startLine+'–'+l.endLine:'L'+l.startLine+'–'+l.endLine;}
    const CITATION_REASON={NUMBER_NOT_ISSUED:'编号未签发',SOURCE_FIELDS_INVALID:'来源关键字段无效',VERSION_MISMATCH:'来源版本不符',HISTORY_NOT_LOADED:'相关历史尚未加载',NUMBER_CONFLICT:'编号冲突',TURN_MISMATCH:'引用轮次不符',SESSION_MISMATCH:'引用会话不符',SOURCE_MISSING:'来源记录缺失'};
    function citationFailure(c){return '引用无法匹配：'+(CITATION_REASON[c.reason]||'来源不可用');}
    function readableAnswer(answer){return answer.prose+'\n\n参考来源\n'+answer.citations.map(c=>'['+c.number+'] '+(c.card?(c.card.title||c.card.fileName||'未命名来源')+' · '+readableLocation(c.card):citationFailure(c))).join('\n');}
    const ANSWER_BUTTON_CSS = `.kw-answer-entry{display:inline-flex;align-items:center;gap:5px;height:30px;padding:3px 8px;border:0;border-radius:8px;background:transparent;color:var(--dsw-alias-text-l2,#727B76);font:inherit;font-size:12px;cursor:pointer}.kw-answer-entry:hover,.kw-answer-entry[aria-pressed=true]{background:var(--dsw-alias-bg-l2,#eef4f0);color:var(--dsw-alias-text-l1,#287E68)}.kw-answer-entry:focus-visible{outline:2px solid var(--dsw-alias-text-success,#287E68);outline-offset:2px}.kw-answer-count{font-size:11px;border-radius:4px;padding:0 3px;background:var(--dsw-alias-bg-l2,#eef4f0)}`;
    function AnswerActions(props){
      const runtime=React.useSyncExternalStore(subscribeRuntime,()=>panelRuntime,()=>null);
      const mounted=runtime?.sidebar.mounted,trees=runtime?.trees||EMPTY_STORE;
      const mountedId=React.useSyncExternalStore(mounted?.subscribe||EMPTY_STORE.subscribe,()=>mounted?.getSnapshot()||null,()=>null);const id=props.sessionId||mountedId;
      const tree=React.useSyncExternalStore(trees.subscribe,()=>trees.snapshot(id),()=>trees.snapshot(id));
      const selection=React.useSyncExternalStore((runtime?.store||EMPTY_STORE).subscribe,()=>runtime?.store.snapshot(id),()=>null);
      const answer=(tree.answers||[]).find(row=>row.assistantMessageId===props.messageId);
      if(!runtime||mountedId!==id||!answer||!answer.citations.length)return null;
      const chosen=selection?.assistantMessageId===answer.assistantMessageId||selection?.answerCitation?.assistantMessageId===answer.assistantMessageId;
      return h(React.Fragment,null,h('style',null,ANSWER_BUTTON_CSS),h('button',{type:'button',className:'kw-answer-entry','aria-label':'查看本回答的 '+answer.citations.length+' 条引用','aria-pressed':chosen,onClick:()=>{if(panelRuntime!==runtime||mounted.getSnapshot()!==id)return;runtime.store.select(id,{kind:'answer',assistantMessageId:answer.assistantMessageId});runtime.sidebar.openTab(PANEL_KIND);}},h('svg',{'aria-hidden':true,width:16,height:16,viewBox:'0 0 24 24',fill:'none',stroke:'currentColor',strokeWidth:1.5},h('path',{d:'M12 5c-3-2-6-2-9-1v15c3-1 6-1 9 1 3-2 6-2 9-1V4c-3-1-6-1-9 1zm0 0v15'})),h('span',null,'来源'),h('span',{className:'kw-answer-count'},answer.citations.length)));
    }
    function excerptKey(card) { return JSON.stringify([card.libraryId,card.relativePath || card.fileName,card.sourceVersion,card.location?.startLine,card.location?.endLine,card.excerpt,card.citationOrigin]); }
    function sourcePreview(card) { return (text(card.excerpt)||'尚未读取正文').slice(0,70).replace(/\s+/g,' ').trim(); }
    function filterSources(groups,query) {
      const needle=query.trim().toLocaleLowerCase();
      return groups.map(group=>({...group,documents:group.documents.filter(doc=>[doc.title,...doc.cards.map(card=>sourcePreview(card))].join(' ').toLocaleLowerCase().includes(needle))})).filter(group=>group.documents.length);
    }
    // Preserve every character; classify display blocks only, never interpret HTML or load resources.
    function readerSegments(content) {
      const parts=[];let offset=0;
      const blocks=/```[\s\S]*?(?:```|(?![\s\S]))|\$\$[\s\S]*?(?:\$\$|(?![\s\S]))|(?:^[^\n]*\|[^\n]*(?:\n|$)){2,}/gm;
      for(const match of content.matchAll(blocks)){
        if(match.index>offset)parts.push({block:false,text:content.slice(offset,match.index)});
        parts.push({block:true,text:match[0]});offset=match.index+match[0].length;
      }
      if(offset<content.length)parts.push({block:false,text:content.slice(offset)});
      return parts;
    }
    const WORKBENCH_CSS = `
      .kw{--kw-bg:var(--dsw-alias-bg-l1,#FAFAF8);--kw-text:var(--dsw-alias-text-l1,#202624);--kw-muted:var(--dsw-alias-text-l2,#727B76);--kw-border:var(--dsw-alias-border-l1,#E5E9E5);--kw-accent:var(--dsw-alias-text-success,#287E68);background:var(--kw-bg);color:var(--kw-text);font:14px/1.6 system-ui,sans-serif;height:100%;min-height:360px;display:flex;flex-direction:column;overflow:hidden;position:relative;width:100%;min-width:0}
      .kw *{box-sizing:border-box}.kw h3{font-size:16px;margin:0}.kw p{margin:4px 0}.kw small{font-size:12px;color:var(--kw-muted)}.kw button,.kw input{font:inherit;color:inherit;border:1px solid var(--kw-border);border-radius:6px;background:transparent;padding:4px 8px}.kw button{cursor:pointer}.kw button:disabled{opacity:.5;cursor:not-allowed}.kw button:focus-visible,.kw summary:focus-visible,.kw input:focus-visible,.kw [role=separator]:focus-visible{outline:2px solid var(--kw-accent);outline-offset:2px}.kw .kw-primary{background:var(--kw-accent);color:var(--dsw-alias-bg-l1,#fff)}
      .kw-tabs{display:flex;gap:8px;padding:8px 16px;border-bottom:1px solid var(--kw-border)}.kw-tabs [role=tab]{flex:1;border:0}.kw-tabs [aria-selected=true]{background:color-mix(in srgb,var(--kw-accent) 10%,transparent);color:var(--kw-accent)}.kw-citation{display:grid;width:100%;grid-template-columns:28px minmax(0,1fr);gap:4px 8px;text-align:left;margin:4px 0;border:0!important}.kw-citation small{grid-column:2}.kw-number{color:var(--kw-accent)}.kw-citation-label{display:block;margin:8px 0;padding:4px 0}.kw select{max-width:100%;width:100%;font:inherit;background:var(--kw-bg);color:var(--kw-text)}.kw-heading{display:flex;align-items:center;gap:8px}.kw-heading h3,.kw-heading strong{flex:1}.kw-controls,.kw-library{padding:12px 16px;border-bottom:1px solid var(--kw-border);flex:none}.kw-controls{max-height:38%;overflow:auto;font-size:12px}.kw-menu button{margin:4px}.kw-budget{font-size:12px!important;border:0!important}.kw-notice{font-size:12px;padding:8px;border-radius:8px;background:var(--dsw-alias-bg-warning,#fff4d8)}.kw-settings{position:absolute;inset:8px;background:var(--kw-bg);border:1px solid var(--kw-border);border-radius:8px;padding:16px;z-index:3;overflow:auto;box-shadow:0 8px 24px #0002}.kw-stepper{display:flex;gap:8px;margin:16px 0}.kw-stepper input{width:72px;text-align:center}
      .kw-split{flex:1;min-height:0;display:flex;flex-direction:column;overflow:hidden}.kw-sources{overflow:auto;padding:12px 16px;min-height:80px;flex:none;scrollbar-gutter:stable}.kw-search{width:100%;margin:8px 0}.kw-sources summary{cursor:pointer}.kw-document{margin:8px 0 8px 8px}.kw-document>summary{display:block;position:relative;padding-left:16px}.kw-document>summary::before{content:'▸';position:absolute;left:0}.kw-document[open]>summary::before{content:'▾'}.kw-doc-title{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.kw-excerpt{display:block;width:100%;text-align:left;margin:4px 0;border:0!important;border-left:2px solid transparent!important}.kw-excerpt[aria-pressed=true]{border-left-color:var(--kw-accent)!important;background:color-mix(in srgb,var(--kw-accent) 10%,transparent)}.kw-preview{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--kw-muted);font-size:12px}.kw-divider{height:8px;flex:none;cursor:row-resize;background:var(--kw-border);touch-action:none}.kw-reader{flex:1;min-height:80px;overflow:auto;padding:16px;background:var(--kw-reader-bg,#fff);scrollbar-gutter:stable}.kw-text{font:inherit;line-height:1.8;white-space:pre-wrap;overflow-wrap:anywhere;overflow:auto;max-width:100%;tab-size:4}.kw-code{white-space:pre;overflow:auto;max-width:100%;font:13px/1.7 ui-monospace,monospace;padding:8px;background:var(--kw-bg);border:1px solid var(--kw-border);border-radius:6px}.kw-reader details{margin-top:8px}.kw-actions{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}.kw-empty{color:var(--kw-muted);padding:16px 0}.kw pre{max-width:100%;overflow:auto}.kw details{transition:background 150ms}[data-ds-dark-theme] .kw{--kw-bg:#1D2420;--kw-reader-bg:#202923;--kw-text:#E6ECE8;--kw-muted:#A3B0A8;--kw-border:#38463D;--kw-accent:#71B8A1}[data-ds-dark-theme] .kw .kw-primary{color:#15221C}[data-ds-dark-theme] .kw .kw-notice{background:#493E22;color:#F2D48A}[data-ds-dark-theme] .kw .kw-failure{background:#352422!important;color:#F2DDD8;border-color:#744A45!important}@media(prefers-reduced-motion:reduce){.kw *{transition:none!important}}
    `;
    function Workbench(props) {
      const {runtime,tree,value}=props;
      const [filter,setFilter]=React.useState(''),[height,setHeight]=React.useState(35),[expanded,setExpanded]=React.useState(false),[notice,setNotice]=React.useState('');
      const [view,setView]=React.useState(value?.kind==='answer'||value?.answerCitation?'answers':'materials');
      React.useEffect(()=>{if(value?.kind==='answer'||value?.answerCitation)setView('answers');else if(value?.kind==='source'||value?.kind==='libraries')setView('materials');},[value]);
      const split=React.useRef(null),reader=React.useRef(null);
      const selected=value?.kind==='source'?sourceCard(value.card):null, key=selected?excerptKey(selected):null;
      React.useEffect(()=>{if(reader.current)reader.current.scrollTop=0;setNotice('');},[key]);
      const copy=async content=>{try{await navigator.clipboard.writeText(content);setNotice('已复制。');}catch{setNotice('复制失败，请手动选择原文复制。');}};
      const focusAnswer=value?.assistantMessageId||value?.answerCitation?.assistantMessageId||(tree.answers||[]).at(-1)?.assistantMessageId;
      const displayedAnswers=(tree.answers||[]).filter(row=>row.assistantMessageId===focusAnswer);
      const groups=filterSources(tree.groups || [],filter), count=(tree.groups || []).reduce((n,g)=>n+g.documents.length,0);
      return h('section',{'aria-label':'知识库来源面板',className:'kw'},h('style',null,WORKBENCH_CSS),
        h(PanelControls,{sessionId:props.sessionId,active:props.active,budget:tree.budget}),
        h('div',{role:'tablist','aria-label':'来源视图',className:'kw-tabs'},...['answers','materials'].map((v,i)=>h('button',{key:v,id:'kw-tab-'+props.sessionId+'-'+v,role:'tab',type:'button','aria-selected':view===v,'aria-controls':'kw-list-'+props.sessionId,tabIndex:view===v?0:-1,onClick:()=>setView(v),onKeyDown:e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();const next=e.key==='Home'?'answers':e.key==='End'?'materials':v==='answers'?'materials':'answers';setView(next);e.currentTarget.parentElement?.querySelector('[id$="-'+next+'"]')?.focus();}}},i?'会话资料':'回答引用'))),
        h('div',{className:'kw-split',ref:split},
          !expanded ? h('section',{className:'kw-sources',style:{height:height+'%'},'aria-label':'本会话来源树',id:'kw-list-'+props.sessionId,role:'tabpanel','aria-labelledby':'kw-tab-'+props.sessionId+'-'+view},
            view==='materials'?h('div',{className:'kw-heading'},h('strong',null,'会话资料'),h('small',null,count+' 文档')):null,
            view==='materials'?h('input',{className:'kw-search',type:'search',placeholder:'搜索本会话来源……','aria-label':'搜索本会话来源',value:filter,onChange:e=>setFilter(e.target.value)}):null,
            tree.pending ? h('small',{role:'status'},'正在取得资料…') : null,
            tree.kind==='loading'?h('p',{role:'status'},'正在加载会话来源…'):null,
            tree.kind==='unavailable'?h('p',{role:'alert'},'会话历史未加载。已有来源仍可查看。'):null,
            tree.updateFailed?h('p',{role:'alert'},'日志重建失败；保留上次有效资料，不保证为最新状态。'):null,
            tree.qualityDegraded?h('p',{role:'status'},'解析质量字段降级为未知：'+tree.qualityDegraded+' 项；已核验来源保留。'):null,
            tree.sourceRejected?h('p',{role:'alert'},'来源关键字段无效：拒绝 '+tree.sourceRejected+' 项。'):null,
            tree.recordFailures?h('p',{role:'alert'},'日志记录重建失败：'+tree.recordFailures+' 项。'):null,
            h('details',null,h('summary',null,'资料操作'),h('button',{type:'button',disabled:!props.active||!runtime.reload,onClick:()=>setNotice(runtime.reload?.(props.sessionId)?'正在重绑会话数据；不会搜索或读取。':'重载次数已达上限或当前不可用，请刷新页面。')},'重新加载面板')),
            view==='answers'?h('section',{'aria-label':'本回答引用'},h('strong',null,'本回答引用'),h('small',null,'正文编号为普通文字；在这里打开精确摘录。'),
              (tree.answers||[]).length>1?h('select',{'aria-label':'选择回答来源',value:focusAnswer||'',onChange:e=>{if(props.active)runtime.store.select(props.sessionId,{kind:'answer',assistantMessageId:e.target.value});}},...(tree.answers||[]).slice(-32).map((answer,index)=>h('option',{key:answer.assistantMessageId,value:answer.assistantMessageId},'回答 '+(index+1)+(answer.questionTitle?' · '+answer.questionTitle:'')))):null,
              ...displayedAnswers.map(answer=>h('details',{key:answer.assistantMessageId,open:true},h('summary',null,'回答 · '+answer.citations.length+' 条编号'),
                ...answer.citations.map(citation=>h('button',{type:'button',key:citation.number,disabled:!props.active||citation.unmatched||!citation.card,className:'kw-citation',onClick:()=>{runtime.store.select(props.sessionId,{kind:'source',card:citation.card,answerCitation:{number:citation.number,assistantMessageId:answer.assistantMessageId}});if(split.current?.clientWidth<360)setExpanded(true);}},h('span',{className:'kw-number'},'['+citation.number+']'),h('span',{className:'kw-doc-title'},citation.card?.title||citationFailure(citation)),citation.card?h('small',null,readableLocation(citation.card)+' · '+(/\.md$/i.test(citation.card.fileName||'')?'Markdown':'文档')):null)),
                h('button',{type:'button',onClick:()=>copy(readableAnswer(answer))},'复制答案'),h('button',{type:'button',onClick:()=>copy(JSON.stringify({sessionId:props.sessionId,...answer},null,2))},'复制审计记录'))),
              !displayedAnswers.some(answer=>answer.citations.length)?h('p',null,'本次已加载回答暂无可匹配编号；检索来源不等于回答依据。'):null):null,

            ...(view==='materials'?groups:[]).map(group=>h('details',{key:group.library,open:true},h('summary',null,group.library),...group.documents.map(doc=>h('details',{key:doc.key,open:true,className:'kw-document'},
              h('summary',null,h('span',{className:'kw-doc-title'},doc.title),h('small',null,(/\.md$/i.test(doc.cards[0]?.fileName || '')?'Markdown':doc.cards[0]?.location?.pageSource==='page-map'?'PDF':'文档')+' · '+doc.cards.length+' 条摘录')),
              ...doc.cards.map(card=>h('button',{key:excerptKey(card),type:'button',className:'kw-excerpt','aria-pressed':key===excerptKey(card),onClick:()=>{if(props.active){runtime.store.select(props.sessionId,{kind:'source',card});if(split.current?.clientWidth<360)setExpanded(true);}}},
                Number.isInteger(card.location?.startLine)?'L'+card.location.startLine+'–'+card.location.endLine:'文献定位',h('span',{className:'kw-preview'},sourcePreview(card)))))))),
            view==='materials'&&!groups.length ? h('p',{className:'kw-empty'},filter?'没有匹配的已返回来源。':'尚无返回来源。工具返回后自动显示在这里。') : null,
            tree.hasMore ? h('small',null,'仅显示已加载的会话历史。') : null,
            ...(tree.failures || []).map((failure,index)=>h('details',{key:index},h('summary',null,'查询未完成 · '+failure.advice.category),h(FailureCard,{failure})))) : null,
          !expanded ? h('div',{role:'separator','aria-label':'调整来源与阅读区高度','aria-orientation':'horizontal','aria-valuemin':20,'aria-valuemax':75,'aria-valuenow':height,tabIndex:0,className:'kw-divider',onKeyDown:e=>{if(['ArrowUp','ArrowDown','Home','End'].includes(e.key)){e.preventDefault();setHeight(v=>e.key==='Home'?20:e.key==='End'?75:Math.max(20,Math.min(75,v+(e.key==='ArrowUp'?-5:5))));}},onPointerDown:e=>e.currentTarget.setPointerCapture(e.pointerId),onPointerMove:e=>{if(e.currentTarget.hasPointerCapture(e.pointerId)&&split.current){const r=split.current.getBoundingClientRect();setHeight(Math.max(20,Math.min(75,(e.clientY-r.top)/r.height*100)));}},onPointerUp:e=>{if(e.currentTarget.hasPointerCapture(e.pointerId))e.currentTarget.releasePointerCapture(e.pointerId);}}) : null,
          h('section',{className:'kw-reader',ref:reader,'aria-label':'摘录阅读区'},selected ? [h('strong',{key:'title'},selected.title || selected.fileName || '原文摘录'),h('small',{key:'position',style:{display:'block'}},readableLocation(selected)),statusRow({...selected,citationId:null,status:{...selected.status,identityDetail:null}}), h('small',null,/\.md$/i.test(selected.fileName||'')?'来源类型：Markdown/笔记；原始论文归属未自动确认。':'来源类型：文档；身份与结论支持分别判断。'),value.answerCitation?h('small',{className:'kw-citation-label'},'引用 ['+value.answerCitation.number+'] · 对应原文摘录'):null,
            (selected.truncationNotices || []).includes('EXCERPT_INCOMPLETE_AT_EOF') ? h('p',{className:'kw-notice'},'已到文件末尾；文本末句可能不完整。') : (selected.truncationNotices || []).some(n=>n.startsWith('EXCERPT_TRUNCATED')) ? h('p',{className:'kw-notice'},'摘录尚未到句末，可能缺少条件或单位。') : null,
            h('div',{key:'text'},readerSegments(text(selected.excerpt) || '本次调用没有返回摘录。').map((part,index)=>h(part.block?'pre':'div',{key:index,className:part.block?'kw-code':'kw-text'},part.text))),
            h('div',{className:'kw-actions'},h('button',{type:'button',onClick:()=>copy(text(selected.excerpt))},'复制摘录'),h('button',{type:'button',onClick:()=>copy((selected.title||selected.fileName||'未命名来源')+' · '+readableLocation(selected))},'复制出处'),h('button',{type:'button',onClick:()=>setExpanded(v=>!v)},expanded?'返回来源树':'展开阅读')),
            h('details',null,h('summary',null,'来源详情与质量提示'),selected.status.identityDetail?h('p',null,'身份详情：'+selected.status.identityDetail):null,h('p',null,'文件：'+(selected.relativePath || selected.fileName || '未知')),h('p',null,'版本：'+(selected.sourceVersion || '未知')),h('p',null,'引用标识：'+(selected.citationId||'未签发')),notices(selected),h('p',null,'打开原件（宿主不支持，未接入）。本面板仅展示已返回内容，不自动补读。')),
            notice ? h('p',{role:'status'},notice) : null] : h('p',{className:'kw-empty'},value.kind==='libraries'?'历史列库：'+value.ids.join('、'):'尚未选择摘录。点击来源树中的摘录阅读。'))));
    }


    /** The tools whose results carry a source-card payload. */
    const TOOL_NAMES = ['knowledge_search', 'knowledge_find_documents', 'knowledge_list_documents', 'knowledge_read', 'knowledge_list_libraries'];

    /**
     * The failure display table. This is a deliberate **copy** of `FAILURE_ADVICE` in `src/failures.ts`:
     * this bundle can only resolve the shell's module table, so the two halves cannot import from each
     * other, and `tests/failure-cards.test.mjs` extracts both copies and asserts they agree.
     *
     * The copy is the point, not an accident: the card resolves every displayed word from **this** table by
     * code, so no text that travelled through the session log can reach the screen. A record can contribute
     * only a code (must be in this table), a `detail` (must match a strict token shape) and a `reference`
     * (must match the id shape).
     */
    const FAILURE_ADVICE = {
      KNOWLEDGE_LIBRARY_NOT_SELECTED: {
        category: '库范围不匹配',
        reason: '这次调用请求的知识库与当前会话选定的知识库不一致，插件在发出请求前就拒绝了它。',
        action: '用 /knowledge-use <库 ID> 把会话切到该库，或让调用改用当前会话已选定的库。',
      },
      KNOWLEDGE_REPEAT_REFUSED: {
        category: '重复失败已熔断',
        reason: '同一调用在连续失败后被插件停止，插件不再为它做任何工作。',
        action: '结束本轮并说明已知与未知，不要重复同一调用；换参数或换库都不会让同一个失败变成成功。',
      },
      KNOWLEDGE_TURN_LIMIT_REACHED: {
        category: '本轮工具调用已达上限',
        reason: '本轮对知识库工具的调用次数达到了插件设定的上限，后续调用不再发出请求、也不启动容器。',
        action: '用现有结果作答并说明缺口；需要继续检索时，在**新的一轮**里再发起。',
      },
      KNOWLEDGE_NOT_CONFIGURED: {
        category: '知识库服务未配置',
        reason: '插件没有拿到可用的服务地址或访问令牌，因此无法发起调用。',
        action: '按部署说明设置回环服务地址与访问令牌（两者都由部署侧的环境提供，面板不持有它们），然后重启宿主。',
      },
      KNOWLEDGE_SERVICE_UNREACHABLE: {
        category: '知识库服务不可达',
        reason: '插件无法连接到知识库服务，请求没有送达。',
        action: '请在知识库面板启动服务，确认可用后继续。',
      },
      KNOWLEDGE_SERVICE_TIMEOUT: {
        category: '知识库服务超时',
        reason: '知识库服务在超时时间内没有返回结果，插件已放弃这次调用。',
        action: '稍后重试；若反复超时，检查服务负载或改用更小的检索范围。',
      },
      KNOWLEDGE_SERVICE_UNAUTHORIZED: {
        category: '知识库服务拒绝鉴权',
        reason: '知识库服务拒绝了随请求发送的凭据。',
        action: '核对部署侧配置的访问令牌与服务端配置是否一致，然后重启宿主。',
      },
      KNOWLEDGE_BUILD_MISMATCH: {
    category: '知识库组件版本不一致',
    reason: '页面、插件、后端或隔离运行时未加载同一构建，本次业务请求未发出。',
    action: '完成整套更新并重启后端与宿主，刷新页面后再试；已有任务仍可取消。',
  },
      KNOWLEDGE_SERVICE_UNAVAILABLE: {
        category: '知识库服务内部错误',
        reason: '知识库服务返回了服务端错误，这次调用没有结果。',
        action: '查看服务端日志后重试；这不代表库中没有相关资料。',
      },
      KNOWLEDGE_REQUEST_CANCELLED: {
        category: '调用已取消',
        reason: '调用在完成前被取消（通常是本轮被停止）。',
        action: '需要该结果时重新发起调用。',
      },
      KNOWLEDGE_SOURCE_CHANGED: {
        category: '原文已变更',
        reason: '这个文件自标识签发之后已经改变，插件不会用旧标识读取新内容。',
        action: '重新检索以获得新的标识，再读取；不要沿用旧标识。',
      },
      KNOWLEDGE_SOURCE_UNAVAILABLE: {
        category: '原文不可用',
        reason: '标识本身有效，但它指向的原文文件当前不可读（已移出库、被替换为链接、超过大小上限或不可访问）。',
        action: '确认该文件仍在库内且未被移动；需要时重建目录后再定位。',
      },
      KNOWLEDGE_QUOTA_EXHAUSTED: {
        category: '额度已用尽',
        reason: '这次调用被额度限制拒绝：服务报告的额度已经用尽（读取会话额度、提供方预算或结果保留容量），具体是其中哪一项由下方说明里的服务码指出。',
        action: '先看说明里的服务码：读取额度到顶时等到新会话或提高限额；提供方预算到顶时必须等配置或新窗口，短暂等待不会恢复。不要针对目录或读取的限额说"本轮其余调用都会失败"，那与搜索锁无关。',
      },
      KNOWLEDGE_RATE_LIMITED: {
        category: '请求被限流（原因未识别）',
        reason: '服务以 HTTP 429 拒绝了这次调用，但**没有**给出本插件能识别的机器码，因此具体原因未识别：既不确定是并发占用，也不确定是读取额度或提供方额度。',
        action: '如实报告"被限流且原因未识别"，等待状态变化后由用户决定是否重试；不要换 FAST/DEEP、不要用 shell 等待、不要据此断定库中没有资料。若服务是新版本，卡片上的服务码会指出具体类别。',
      },
      KNOWLEDGE_QUERY_NOT_TERMINAL: {
        category: '作业尚未收口', reason: '作业或本地写入者尚未结束，不能确认终态或释放占用。',
        action: '保留当前作业并查询状态；需要停止时使用取消入口，不要立即重送查询。',
      },
      KNOWLEDGE_QUERY_RESULT_RELEASED: {
        category: '结果已释放', reason: '这次逻辑提交已执行，结果已确认或租约到期释放；重送不会再次执行。',
        action: '查看已保存的结果；确需新查询时发起新的提交，不要重用旧 requestId。',
      },
      KNOWLEDGE_QUERY_BUSY: {
        category: '当前已有查询在运行',
        reason: '服务一次只允许一个查询在运行，因此这次提交没有被接纳；这是并发占用，不是额度耗尽，也不是库中没有资料。',
        action: '不要换模式（FAST/DEEP）重发，也不要靠 shell 等待：占用不会因此解除。按下方说明里的作业状态有界地再查一次，或等待正在运行的查询结束后再提交。',
      },
      KNOWLEDGE_REQUEST_REJECTED: {
        category: '请求被拒绝',
        reason: '知识库服务拒绝了这次请求的参数或目标（例如库不存在或请求不合法）。',
        action: '核对该调用的库 ID 与参数，然后重试。',
      },
      KNOWLEDGE_READ_RANGE_INVALID: {
        category: '读取范围超出文件',
        reason: '这次读取请求的起始行超出了该文件的实际行数，服务在读取内容前拒绝了它，没有返回任何正文。',
        action: '按下方给出的文件行数改小 startLine（1 基，且不超过该行数）后重试一次；也可以用上一条结果里的短引用续读，不必猜行号。',
      },
      KNOWLEDGE_READ_REQUEST_INVALID: {
        category: '读取请求参数不合法',
        reason: '这次读取请求的标识或参数不构成一次合法的读取，服务在打开文件前拒绝了它。',
        action: '按下方给出的固定原因修正参数：线号问题改 startLine；标识问题改用刚返回的短引用重新定位。',
      },
      KNOWLEDGE_REF_KIND_CONFLICT: {
        category: '引用类型与定位参数冲突',
        reason: '这次调用同时给了"续读引用"（nextRef，绑定到窗口结束位置）和 startLine（要求从另一行开始），两者是互相矛盾的指令，插件在发出请求前就拒绝了它。',
        action: '要接着上次的位置读，就去掉 startLine；要跳到某一行，就用同一文件的"文档引用"（readRef，卡片上会给出可用的那个）。',
      },
      KNOWLEDGE_PARAMETER_OUT_OF_RANGE: {
        category: '参数超出允许范围',
        reason: '这次调用的某个参数超出了插件声明的取值范围，插件在发出请求前就拒绝了它，服务没有收到任何内容。',
        action: '按工具说明中给出的整数范围修正该参数后重试；插件不会把越界值静默改成边界值。',
      },
      KNOWLEDGE_READ_REF_UNKNOWN: {
        category: '读取引用已失效',
        reason: '这个短引用在当前插件实例里找不到对应项——引用是进程内、有界的，宿主或插件重载、条目被淘汰、或者它本来就不是本插件签发的，都会是这个结果。',
        action: '重新用 knowledge_find_documents 或 knowledge_search 定位该文件以取得新引用；不要凭记忆改写引用，它不会解析成别的文件。',
      },
      KNOWLEDGE_READ_REF_FOREIGN: {
        category: '读取引用不属于本会话',
        reason: '这个短引用是为另一个会话、工作区或知识库签发的，插件不会把它重定向到当前库里的同名文件。',
        action: '在本次会话范围内重新定位该文件；跨会话共享引用是不被支持的。',
      },
      KNOWLEDGE_HANDLE_INVALID: {
        category: '文件标识无效',
        reason: '随调用提交的文件标识不是本服务为该工作区与该库签发过的标识（被改写、被截断，或属于另一个工作区/库）；服务按签名拒绝，因此没有读取任何内容。',
        action: '重新定位或检索该文件以取得新的标识/短引用；这次失败不代表文件已删除或库中没有该资料。',
      },
      KNOWLEDGE_UNKNOWN: {
        category: '未知错误',
        reason: '这次调用以插件没有归类的错误结束，因此不提供具体原因。',
        action: '把下面的关联 ID 提供给维护者，并附上复现步骤。',
      },
    };

    /**
     * The declared sets the host half uses. A rendered "说明" line is assembled from members of these sets and
     * from numbers; there is no free-text field and no pattern to pass, so a secret cannot be hidden in one.
     */
    const SAFE_REFERENCE = /^k-[0-9a-z]{8}$/;
    /** The short-reference shape, mirroring `src/refs.ts`; the only non-numeric field a detail may render. */
    const SAFE_SHORT_REF = /^kref-[A-Za-z0-9_-]{8,32}$/;
    const SAFE_HOST_CODE = /^[A-Za-z0-9._:-]{1,40}$/;

    /**
     * The declared sets the host half uses. Membership in one of these is the *only* way a value becomes part
     * of a rendered sentence - there is no free-text field and no pattern to pass.
     * `tests/failure-cards.test.mjs` asserts both halves declare the same members.
     */
    const FAILURE_LAYERS = ['plugin', 'transport', 'service', 'host'];
    const SERVICE_CODES = ['BUILD_MISMATCH', 'RUNTIME_BUILD_MISMATCH', 'SOURCE_VERSION_CHANGED', 'READ_SOURCE_VERSION_MISMATCH', 'READ_HANDLE_INVALID',
      'READ_HANDLE_FOREIGN', 'READ_FILE_UNAVAILABLE', 'READ_REQUEST_INVALID', 'READ_RANGE_INVALID',
      'READ_LIMIT_EXCEEDED', 'RESULT_CAPACITY', 'CATALOG_UNAVAILABLE', 'CATALOG_UNREADABLE',
      'CATALOGUE_REQUEST_INVALID', 'CATALOGUE_CHANGED', 'DOCUMENT_NOT_READABLE', 'DOCUMENT_CHANGED',
      'NO_CATALOG_MATCH', 'UNKNOWN_LIBRARY',
      // The query lifecycle's own codes. They exist because one HTTP 429 used to mean all of these at once:
      // a job already running, a full result store, a spent provider budget, and a spent read budget are
      // different facts with different next steps, and the client now reports which one it was told.
      'QUERY_BUSY', 'PROVIDER_QUOTA_EXHAUSTED', 'UNKNOWN_RATE_LIMIT',
      'WORKER_NO_RESULT', 'WORKER_EXITED', 'ISOLATION_UNAVAILABLE', 'QUERY_REQUEST_CONFLICT', 'QUERY_REQUEST_CAPACITY', 'QUERY_RESULT_RELEASED', 'QUERY_NOT_TERMINAL',
      'QUERY_REQUEST_INVALID', 'REAL_DATA_UNAUTHORIZED'];
    const FAILURE_TOOLS = ['knowledge_list_libraries', 'knowledge_search', 'knowledge_find_documents',
      'knowledge_list_documents', 'knowledge_read'];
    /**
     * The parameter names the host half may report. A closed set for the same reason as the tool set: a
     * refusal names the offending parameter, and it must have no way to name anything else.
     */
    const FAILURE_PARAMETERS = ['libraryId', 'query', 'mode', 'doi', 'title', 'authors', 'year', 'fileName',
      'limit', 'offset', 'catalogueVersion', 'ref', 'fileHandle', 'cursor', 'sourceVersion', 'startLine',
      'maxChars', 'maxLines', 'overlapLines'];
    /**
     * The recoverable-read facts, mirrored from `READ_REFUSAL_REASONS` / `READ_SUGGESTED_ACTIONS` in
     * `src/failures.ts` (and, on the service side, `engine/reader.py`). A member of one of these sets is the
     * only thing the corresponding line can render; anything else is dropped rather than displayed.
     *
     * The trailing members are the plugin's own (`PLUGIN_REF_REASONS` / `PLUGIN_REF_ACTIONS` there): facts the
     * plugin decides before any request, which the service can never send. They are declared in the same sets
     * here because a card renders by membership, and a refusal is a refusal wherever it was decided.
     */
    const READ_REFUSAL_REASONS = ['START_LINE_INVALID', 'START_LINE_PAST_END', 'START_OUTSIDE_FILE',
      'CURSOR_OUTSIDE_FILE', 'IDENTIFIER_MISSING', 'IDENTIFIER_CONFLICT', 'PARAMETER_TYPE_INVALID',
      'CONTINUATION_REF_WITH_START_LINE'];
    const READ_SUGGESTED_ACTIONS = ['CORRECT_START_LINE', 'LOCATE_AGAIN', 'CORRECT_PARAMETER',
      'USE_DOCUMENT_REF', 'DROP_START_LINE'];
    /**
     * The submission lifecycle, mirrored from `QUERY_PHASES` / `JOB_STATES` in `src/failures.ts`.
     *
     * `JOB_STATES` is what the **service** may answer with, so membership here is also the check that a state
     * this build does not know is dropped rather than rendered raw.
     */
    const QUERY_PHASES = ['submitting', 'accepted', 'polling', 'cancelling', 'completed', 'failed',
      'cancelled', 'unknown'];
    const JOB_STATES = ['received', 'accepted', 'running', 'cancelling', 'completed', 'failed',
      'cancelled', 'unknown'];
    /** The request id shape, mirroring `src/client.ts` / `SAFE_REQUEST_ID` in `src/failures.ts`. */
    const SAFE_REQUEST_ID = /^kq-[0-9a-z]{8,32}$/;

    /** A member of a declared set, a safe integer, or null. Nothing else can become a rendered value. */
    function member(value, set) {
      return typeof value === 'string' && set.indexOf(value) !== -1 ? value : null;
    }
    function count(value) {
      return typeof value === 'number' && Number.isSafeInteger(value) ? value : null;
    }
    /**
     * A line number or a line count: a non-negative integer, or null.
     *
     * Separate from `count` because a negative number is a valid integer and would otherwise render as if it
     * had been a real line - "请求起始行 -3" is not a statement about any file.
     */
    function line(value) {
      const number = count(value);
      return number !== null && number >= 0 ? number : null;
    }

    /**
     * The rendered context line, built by THIS half from typed fields.
     *
     * The host half sends `{layer, httpStatus, serviceCode, tool, parameter, chain, occurrences, used, max,
     * evidence, documents, reason, requestedStartLine, fileLines, suggestedAction}` and nothing else, so this
     * function has no text input to sanitise: every part of the sentence comes from a member of a set above or
     * from a number. A value that is not a member is dropped, not cleaned.
     */
    function describeDetail(raw) {
      const detail = raw && typeof raw === 'object' ? raw : {};
      const parts = [];
      const status = count(detail.httpStatus);
      if (status !== null) parts.push('HTTP ' + status);
      const serviceCode = member(detail.serviceCode, SERVICE_CODES);
      if (serviceCode) parts.push('服务码 ' + serviceCode);
      const layer = member(detail.layer, FAILURE_LAYERS);
      if (layer && layer !== 'plugin') parts.push('层 ' + layer);
      const tool = member(detail.tool, FAILURE_TOOLS);
      if (tool) parts.push('工具 ' + tool);
      const parameter = member(detail.parameter, FAILURE_PARAMETERS);
      if (parameter) parts.push('参数 ' + parameter);
      // The recoverable-read facts: the fixed reason, the file's own line count and the suggested action.
      // Each is a member of a set above or a non-negative integer, so this line is assembled rather than
      // sanitised - the same rule as every other part of this sentence.
      const reason = member(detail.reason, READ_REFUSAL_REASONS);
      if (reason) parts.push('原因 ' + reason);
      const requestedStartLine = line(detail.requestedStartLine);
      if (requestedStartLine !== null) parts.push('请求起始行 ' + requestedStartLine);
      const fileLines = line(detail.fileLines);
      if (fileLines !== null) parts.push('文件行数 ' + fileLines);
      const suggestedAction = member(detail.suggestedAction, READ_SUGGESTED_ACTIONS);
      if (suggestedAction) parts.push('建议动作 ' + suggestedAction);
      // The one rendered value that is not a number or a set member: a short reference, which must match the
      // reference shape to appear at all. It is what makes the conflict refusal actionable on the card too.
      const suggestedRef = typeof detail.suggestedRef === 'string' && SAFE_SHORT_REF.test(detail.suggestedRef)
        ? detail.suggestedRef : null;
      if (suggestedRef) parts.push('可改用引用 ' + suggestedRef);
      const chain = member(detail.chain, ['identical', 'consecutive']);
      const occurrences = count(detail.occurrences);
      if (chain && occurrences !== null) parts.push(chain + ' × ' + occurrences);
      const used = count(detail.used);
      const max = count(detail.max);
      if (used !== null && max !== null) parts.push('本轮已用 ' + used + ' / 上限 ' + max);
      // The deterministic half of the closing story: what the stopped turn had actually obtained.
      const evidence = count(detail.evidence);
      const documents = count(detail.documents);
      const reads = count(detail.reads);
      if (evidence !== null || documents !== null || reads !== null) {
        parts.push('本轮已取得 检索结果 ' + (evidence || 0) + ' 条 / 目录项 ' + (documents || 0)
          + ' 条 / 读取窗口 ' + (reads || 0) + ' 个');
      }
      // The submission's own lifecycle, when the failure happened around a query. Every field is a member of
      // a set above or the client's own request id, which must match the id shape to render at all - so this
      // line says which submission is unresolved without any text being able to travel through it.
      const query = detail.query && typeof detail.query === 'object' ? detail.query : null;
      if (query) {
        const facts = [];
        if (typeof query.requestId === 'string' && SAFE_REQUEST_ID.test(query.requestId)) {
          facts.push('请求 ID ' + query.requestId);
        }
        const phase = member(query.phase, QUERY_PHASES);
        if (phase) facts.push('阶段 ' + phase);
        const jobState = member(query.jobState, JOB_STATES);
        if (jobState) facts.push('作业状态 ' + jobState);
        if (typeof query.cancellationConfirmed === 'boolean') {
          facts.push(query.cancellationConfirmed ? '取消已确认' : '取消未确认');
        }
        if (facts.length) parts.push(facts.join('，'));
      }
      return parts.length ? parts.join('｜') : null;
    }

    /**
     * The failure a settled call should be shown as, or null when there is none.
     *
     * Two sources, checked in order:
     *
     * 1. `meta.failure` — written by this plugin when it **stopped** a call itself. A stop has to travel as a
     *    successful result to reach the host's `concludeTurn()`, so this is where its record lives.
     * 2. `block.error.code` — the host's structured `{name, code}` for a **thrown** failure. That is the
     *    standard path, and the reason every failure is thrown as a `HarnessError` rather than a plain
     *    `Error` (a plain error carries no code).
     *
     * A missing or unrecognised code still produces a card: an unknown failure must never be silently
     * rendered as an empty successful row.
     */
    function safeBudget(value) {
      if(!value || !Number.isSafeInteger(value.used) || value.used<0 || !Number.isSafeInteger(value.max) || value.max<1 || value.max>30)return null;
      const result={used:value.used,max:value.max};
      if(value.activity?.scope==='turn' && ['attempts','httpRequests','searches','reads'].every(k=>Number.isSafeInteger(value.activity[k]) && value.activity[k]>=0))
        result.activity={scope:'turn',attempts:value.activity.attempts,httpRequests:value.activity.httpRequests,searches:value.activity.searches,reads:value.activity.reads};
      return result;
    }
    function failureOf(block) {
      if (!block || block.kind !== 'tool-result' || block.parentCallId !== undefined) return null;
      const meta = block.meta && typeof block.meta === 'object' ? block.meta : null;
      let stored = meta && meta.failure && typeof meta.failure === 'object' ? meta.failure : null;
      if (!stored && (block.isError === true || block.error)) {
        const match=(toolText(block) || '').match(/KNOWLEDGE_FAILURE_FACTS=(\{[^\n]{1,8192}\})/);
        try {const value=JSON.parse(match?.[1] || 'null');if(value && Object.prototype.hasOwnProperty.call(FAILURE_ADVICE,value.code)
          && (!block.error?.code || value.code===block.error.code))stored=value;} catch {}
      }
      const storedCode = stored && typeof stored.code === 'string' ? stored.code : null;
      const hostCode = block.error && typeof block.error.code === 'string' ? block.error.code : null;
      const code = storedCode && Object.prototype.hasOwnProperty.call(FAILURE_ADVICE, storedCode) ? storedCode
        : hostCode && Object.prototype.hasOwnProperty.call(FAILURE_ADVICE, hostCode) ? hostCode : null;
      // The call id is a host-assigned handle for exactly this call and appears in the session log, so it is
      // a real correlation id when the plugin has none of its own to offer.
      const callId = typeof block.callId === 'string' && SAFE_HOST_CODE.test(block.callId) ? block.callId.slice(0, 40) : null;
      const reference = stored && typeof stored.reference === 'string' && SAFE_REFERENCE.test(stored.reference)
        ? stored.reference
        : callId;
      if (code) {
        return {
          code,
          advice: stored?.detail?.query?.cancellationConfirmed === false && !['completed','failed','cancelled'].includes(stored?.detail?.query?.phase)
            ? {...FAILURE_ADVICE[code],action:'先检查本会话作业状态并恢复取消确认；未确认收口前不要立即重发。'}
            : FAILURE_ADVICE[code],
          detail: describeDetail(stored && stored.detail),
          budget: safeBudget(stored?.budget) || safeBudget(stored?.detail),
          reference,
          // The host records `name`/`code` for a thrown failure; neither is a message, and both are bounded.
          hostCode: code === 'KNOWLEDGE_UNKNOWN' ? null : (hostCode || null),
        };
      }
      // `meta.failure` being present at all is the plugin saying "this call was stopped". An unrecognised
      // code inside it is still a failure, so it renders the generic statement rather than falling through to
      // the source path and being reported as an empty result.
      if (stored || block.isError || block.error) {
        return { code: 'KNOWLEDGE_UNKNOWN', advice: FAILURE_ADVICE.KNOWLEDGE_UNKNOWN, detail: null, reference, hostCode: null };
      }
      return null;
    }

    const IDENTITY_LABEL = {
      verified: '身份已核验',
      partial: '身份部分核验',
      conflict: '身份存在冲突',
      undetermined: '身份未确定',
      'not-assessed': '身份未核验',
    };

    const ORIGIN_LABEL = {
      engine: '引擎原文片段',
      'engine-fragment': '引擎片段',
      'source-window': '父进程窗口（非最小片段）',
      read: '本次读取',
      catalogue: '目录定位',
    };

    /**
     * The persisted `presentationMeta` payload of one settled call, or null.
     *
     * A card is only drawn for a settled, successful ROOT call: a running call has no result yet, a
     * failed one has no sources, and a nested dispatch (parentCallId set) is rendered by its own parent
     * row rather than as a duplicate card here.
     */
    function readPayload(block) {
      if (!block || block.kind !== 'tool-result' || block.isError || block.parentCallId !== undefined) return null;
      const meta = block.meta;
      if (!meta || typeof meta !== 'object' || !Array.isArray(meta.sources)) return null;
      const sources=[],rejectedSources=[];for(const raw of meta.sources){const card=sourceCard(raw);if(card)sources.push(card);else rejectedSources.push({citationId:/^ksrc-[a-f0-9]{16}$/.test(raw?.citationId||'')?raw.citationId:null});}return {...meta,sources,rejectedSources,invalidSources:rejectedSources.length};
    }

    /** Plain-text-safe string, or null. Never trusts a field to be the type it should be. */
    function text(value) {
      return typeof value === 'string' && value.trim() !== '' ? value : null;
    }

    /** One line of a card: label plus value, both plain text children. */
    function field(label, value, key) {
      return h('div', { key, style: S.field },
        h('span', { style: S.label }, label),
        h('span', { style: S.value }, value));
    }

    /** The status axes, rendered as three separate statements so they cannot be read as one verdict. */
    function statusRow(card) {
      const citation = /^ksrc-[a-f0-9]{16}$/.test(card.citationId || '') ? card.citationId : null;
      const status = card.status || {};
      const identity = IDENTITY_LABEL[status.identity] || String(status.identity || '未核验');
      const identityDetail = text(status.identityDetail);
      return h('div', { style: S.statusRow },
        citation ? h('span',null,'来源标识：'+citation) : null,
        h('span', { style: S.axisIdentity },
          identity, identityDetail ? '（' + identityDetail + '）' : ''),
        h('span', { style: status.locationVerified ? S.axisOk : S.axisUnknown },
          status.locationVerified ? '位置已核验' : '位置未核验'),
        h('span', { style: S.axisUnknown }, '支持：未评估'));
    }

    /**
     * Where the quote sits, and the reason when no page can be given.
     *
     * Only a map the record vouched for produces a page. A line of the document that looks like the
     * extraction's marker is shown at most as a diagnostic hint explicitly marked "未采信": it is never a
     * page and never a jump target, because any document can contain such a line.
     */
    function locationText(card) {
      const location = card.location;
      // Two different facts, two different sentences. "No location because this call did not read" is only
      // true when the call really returned no window; a call whose catalogue entry refused the read says so,
      // because the old wording covered a refusal with "not read yet".
      const notices = Array.isArray(card.truncationNotices) ? card.truncationNotices : [];
      if (!location) {
        if (notices.indexOf('DOCUMENT_NOT_READABLE') !== -1) {
          return '位置：本次未读取到正文（目录定位认为该文件当前不可读），因此没有位置信息';
        }
        return '位置：本次调用尚未读取，无位置信息';
      }
      const lines = '抽取文本第 ' + location.startLine + '–' + location.endLine + ' 行';
      let clause;
      if (location.kind === 'pdf-page' && location.pdfPage !== null && location.pdfPage !== undefined) {
        // One number would describe only the first line while reading as if it covered the range, so a
        // window that crosses a page boundary says so.
        const end = location.pdfPageEnd === null || location.pdfPageEnd === undefined ? location.pdfPage : location.pdfPageEnd;
        clause = end === location.pdfPage ? 'PDF 第 ' + location.pdfPage + ' 页'
                                         : 'PDF 第 ' + location.pdfPage + '–' + end + ' 页，跨页';
        clause += '，来自抽取映射';
      } else if (location.pageSource === 'map-gap') {
        clause = '抽取映射未覆盖该行（映射缺口）';
      } else if (location.pageSource === 'map-unusable') {
        clause = '页码映射不可用（与当前文本/PDF 版本不匹配或已损坏），不回退到正文标记';
      } else if (location.pageSource === 'unmapped') {
        clause = '页码映射未建立（该 PDF 尚无抽取映射）';
      } else if (location.pageSource === 'not-pdf') {
        clause = '非 PDF 来源，只给行号';
      } else {
        clause = '无页码';
      }
      const hint = location.pageHint === null || location.pageHint === undefined
        ? ''
        : '；正文另有一行疑似标记称第 ' + location.pageHint + ' 页，未采信';
      // Only say "text coordinates are all there is" when that is true: appending it to a mapped page would
      // contradict the page in the same breath.
      const coordinate = location.pdfPage === null || location.pdfPage === undefined
        ? '，只能给抽取文本坐标'
        : '，行号坐标空间为抽取文本';
      return '位置：' + lines + '（' + clause + coordinate + hint + '）';
    }

    /** The truncation and refusal notices, verbatim from the card. Absent means absent. */
    function notices(card) {
      const extra=[];
      if(card.contentWarnings?.includes('POSSIBLE_MATH_LOSS'))extra.push('疑似数学内容缺失；丢失层未知，不补造公式');
      if(['metadata','body','code','references','unknown'].includes(card.structureType))extra.push('窗口结构：'+card.structureType+'（不代表支持结论）');

      const list = Array.isArray(card.truncationNotices) ? card.truncationNotices.filter(t => typeof t === 'string') : [];
      if (!card.truncated && list.length === 0 && !extra.length) return null;
      return h('div', { style: S.warning },
        '截断/质量提示：' + [...list,...extra].join('、'),
        card.truncated && list.length === 0 ? '（结果被截断，以上不是全部）' : '');
    }

    /** One candidate row, for "查看候选差异": identity, why it matched, why it might not. */
    function candidateRow(card, index) {
      const matched = Array.isArray(card.matched) ? card.matched : [];
      const mismatched = Array.isArray(card.mismatched) ? card.mismatched : [];
      const conflicts = Array.isArray(card.conflicts) ? card.conflicts : [];
      return h('div', { key: 'candidate-' + index, style: S.candidate },
        h('div', { style: S.candidateHead },
          (card.title || card.fileName || card.documentId || '候选 ' + (index + 1))),
        h('div', { style: S.sub },
          '身份：' + (IDENTITY_LABEL[card.status && card.status.identity] || '未核验')
          + '｜匹配：' + (matched.length ? matched.join('、') : '无')
          + '｜不一致：' + (mismatched.length ? mismatched.join('、') : '无')
          + (conflicts.length ? '｜冲突记录 ' + conflicts.length + ' 条' : '')),
        h('div', { style: S.sub },
          '文件 ' + (card.fileName || '（未知）')
          + (card.sourceVersion ? '｜版本 ' + String(card.sourceVersion).slice(0, 12) : '｜未签发版本（本次不可读）')));
    }

    /**
     * The failure card.
     *
     * Deliberately unlike the source card: no excerpt, no location, no status axes, no "引用已核验" - a call
     * that returned no sources has nothing to verify, and borrowing the success vocabulary would be a lie.
     * Every word comes from the table above; the record supplied a code, at most a short detail, and a
     * reference id.
     */
    function FailureCard(props) {
      const failure = props.failure;
      const children = [
        h('div', { key: 'head', style: S.head },
          h('strong', { style: S.title }, '知识库调用失败：' + failure.advice.category),
          h('span', { style: S.failureBadge }, '失败')),
        // The machine-readable code, shown as itself. It is a member of the closed table above — a record whose
        // code is not in that table never reaches this component — so this line can carry no text that travelled
        // through the session log. It was missing until a real run: the ceiling card said "本轮已用 7 / 上限 6"
        // and nothing else, so a reader could not name the failure the plugin had already recorded.
        field('错误码', failure.code, 'code'),
        field('类别', failure.advice.category, 'category'),
        field('原因', failure.advice.reason, 'reason'),
        field('建议', failure.advice.action, 'action'),
      ];
      if (failure.detail) children.push(field('说明', failure.detail, 'detail'));
      if (failure.reference) children.push(field('关联 ID', failure.reference, 'reference'));
      if (failure.hostCode) children.push(field('宿主错误码', failure.hostCode, 'host-code'));
      children.push(h('div', { key: 'note', style: S.sub },
        '本次调用没有返回任何来源，也没有可核验的引用；上面的类别与建议不改变这一点。'));
      return h('div', { className:'kw-failure', style: S.cardFailure }, children);
    }

    /** One card body. All state is local and display-only; nothing here can reach the Agent. */
    function CardBody(props) {
      const card = props.card;
      const [openContext, setOpenContext] = React.useState(false);
      const [openCandidates, setOpenCandidates] = React.useState(false);
      const all = props.all;
      const origin = ORIGIN_LABEL[card.citationOrigin] || (card.citationOrigin ? String(card.citationOrigin) : '未标注');

      const children = [
        h('div', { key: 'head', style: S.head },
          h('strong', { style: S.title }, card.title || '（目录无标题）'),
          // The badge states what this call produced, from the notice list the service actually sent: the
          // previous test read a `readable` field that no card carries, so it always said "来源" - true by
          // luck for a read, wrong for a catalogue entry that could not be read.
          h('span', { style: S.badge },
            (Array.isArray(card.truncationNotices) && card.truncationNotices.indexOf('DOCUMENT_NOT_READABLE') !== -1)
              ? '目录命中｜不可读' : '来源')),
        h('details',{key:'audit'},h('summary',null,'定位与审计详情'),
          field('文件', (card.fileName || card.relativePath || '未知'), 'file'),
          field('知识库', String(card.libraryId || '未知'), 'library'),
          field('来源', origin + '｜版本 ' + (card.sourceVersion ? String(card.sourceVersion).slice(0, 12) : '（无）'), 'version'),
          field('引用标识',card.citationId||'未签发','citation'),
          field('解析质量', formatParseQuality(card.parseQuality), 'quality')),
        h('div', { key: 'location', style: S.field }, h('span', { style: S.label }, '位置'), h('span', { style: S.value }, locationText(card))),
        statusRow({...card,citationId:null}),
        h('blockquote', { key: 'excerpt', style: S.quote }, text(card.excerpt) || '（本次调用没有包含摘录）'),
      ];
      const notice = notices(card);
      if (notice) children.push(notice);

      if (props.openPanel) children.push(h('button', {
        key: 'open-source-panel', type: 'button', style: S.button,
        onClick: props.openPanel, 'aria-label': '在右栏查看来源',
      }, '在右栏查看来源'));
      const isRead = card.citationOrigin === 'read';
      children.push(h('div', { key: 'actions', style: S.actions },
        h('button', {
          type: 'button', style: S.button,
          onClick: () => setOpenContext(v => !v),
          // The label says what it does: it expands content this call ALREADY returned. It is not a read,
          // and calling it "context" invited exactly that misreading.
        }, openContext ? '收起已返回内容' : '展开已返回内容'),
        h('button', {
          type: 'button', style: S.button,
          onClick: () => setOpenCandidates(v => !v),
          disabled: all.length < 2,
        }, '查看候选差异（' + all.length + '）'),
        h('button', {
          type: 'button', style: S.buttonDisabled, disabled: true,
          title: '宿主没有“用默认程序打开某个文件”的能力：它的 open-in-app 只能把某个已存在的目录交给资源管理器/编辑器/终端，'
            + '对文件无效，交给它一个文件夹又会把整个目录暴露给读者，因此本轮保持禁用而不做不安全绕过。',
        }, '打开原件（宿主不支持，未接入）')));

      // The other concept, kept on its own line and never a button, so "读取" and "展开" cannot be confused:
      // only a real `knowledge_read` call is a read, and only the main Agent can make one.
      children.push(h('div', { key: 'read-note', style: S.sub },
        isRead
          ? '本次读取返回的窗口（knowledge_read 已由主 Agent 发起）。继续读取需主 Agent 再次调用 knowledge_read。'
          : '本次检索只返回上述摘录，未读取原文；读取原文需主 Agent 调用 knowledge_read。本面板不发起任何请求。'));

      if (openContext) {
        children.push(h('div', { key: 'context', style: S.panel },
          h('div', null, '以下内容就是这次调用的返回值，展开不向服务发起任何请求，也不会把原文送进对话。'),
          card.excerpt ? h('pre', { style: S.pre }, card.excerpt) : null,
          card.location && card.location.startChar !== undefined
            ? h('div', { style: S.sub }, '码点偏移：' + card.location.startChar + '–' + card.location.endChar + '（Unicode 码点，0 基半开）')
            : null));
      }
      if (openCandidates) {
        children.push(h('div', { key: 'candidates', style: S.panel },
          h('div', null, '本次调用返回的候选（不自动选取第一个）：'),
          all.map(candidateRow)));
      }
      return h('div', { style: S.card }, children);
    }

    /**
     * The tool text of one settled call, as plain text, or null.
     *
     * The library-list card needs the tool's own output to say which libraries it listed, and the host hands it
     * over as content parts. Only text parts are read, and nothing is executed or parsed as anything but JSON.
     */
    function toolText(block) {
      const content = block && block.content;
      if (typeof content === 'string') return content;
      if (Array.isArray(content)) {
        const parts = content.filter(part => part && part.type === 'text' && typeof part.text === 'string');
        return parts.length ? parts.map(part => part.text).join('') : null;
      }
      return null;
    }

    /** A library id, bounded and printable; anything else is dropped rather than shown. */
    function libraryId(value) {
      if (typeof value !== 'string') return null;
      const trimmed = value.trim();
      return trimmed !== '' && trimmed.length <= 64 ? trimmed : null;
    }

    /**
     * The card for a call that lists libraries.
     *
     * It is not a source card and it is not a zero-result card: listing the libraries this session may use
     * produces no sources and cannot "miss" a document, so the zero-result wording was wrong for it — a real run
     * showed three "零结果 / 定位未命中" cards for three successful list calls. Everything shown here comes from
     * the call's own output, and a list that cannot be read says so instead of guessing.
     */
    function LibraryListCard(props) {
      const block = props.block;
      let parsed = null;
      const raw = toolText(block);
      if (raw) { try { const value = JSON.parse(raw); if (value && typeof value === 'object') parsed = value; } catch { parsed = null; } }
      const ids = Array.isArray(parsed && parsed.libraries)
        ? parsed.libraries.map(entry => libraryId(entry && entry.id)).filter(Boolean) : [];
      const selected = libraryId(parsed && parsed.selectedLibraryId);
      const children = [
        h('div', { key: 'head', style: S.head },
          h('strong', { style: S.title }, '知识库：本次调用是列出知识库'),
          h('span', { style: S.badge }, '列表')),
        ids.length
          ? field('本次列出', ids.join('、'), 'libraries')
          : field('本次列出', '（输出中没有可识别的库列表）', 'libraries'),
        field('本会话范围', selected || '未设置', 'selected'),
        h('div', { key: 'note', style: S.sub },
          '列出知识库不产生来源，也谈不上"零命中"：这次调用不检索、不读取任何原文。引用与来源只会出现在检索或读取的卡片上。'),
      ];
      const open = panelOpener(props, { kind: 'libraries', ids, selected });
      if (open) children.push(h('button', { type: 'button', style: S.button,
        onClick: open, 'aria-label': '在右栏查看知识库状态' }, '在右栏查看知识库状态'));
      return h('div', { style: S.card }, children);
    }

    /** The registered toolview. Falls back to the shipped generic row when there is no payload. */
    function KnowledgeSourceCard(props) {
      // A failure is checked first and never depends on `meta.sources`: a stopped or refused call has no
      // sources and no citation, so a card keyed on them could not describe it.
      const failure = failureOf(props.block);
      if (failure) return h(FailureCard, { failure });
      const payload = readPayload(props.block);
      if (payload === null) return null;
      const toolName = props.block && props.block.call && typeof props.block.call.name === 'string'
        ? props.block.call.name : null;
      const all = payload.sources;
      if (all.length === 0) {
        // An empty source list means "no sources" for a call that looks for them. A call that lists libraries
        // never has sources, so it gets its own card instead of the zero-result sentence.
        if (toolName === 'knowledge_list_libraries') {
          return h(LibraryListCard, { block: props.block, sessionId: props.sessionId });
        }
        return h('div', { style: S.card },
          h('div', { style: S.head }, h('strong', { style: S.title }, '知识库：本次没有来源')),
          h('div', { style: S.sub }, '零结果 / 定位未命中。这不代表库中没有该资料，只是本次没有定位到。'));
      }
      return h('div', null, all.map((card, index) =>
        h(CardBody, { key: (props.callId || 'call') + '-' + index, card: card, all: all,
          openPanel: panelOpener(props, { kind: 'source', card: {
            libraryId: card.libraryId, title: card.title, fileName: card.fileName,
            relativePath: card.relativePath, excerpt: card.excerpt,
            location: card.location, status: card.status, sourceVersion: card.sourceVersion,citationId:card.citationId,
            citationOrigin: card.citationOrigin, parseQuality: card.parseQuality,
            truncated: card.truncated, truncationNotices: card.truncationNotices,
          } }),
        })));
    }

    const S = {
      card: { border: '1px solid var(--dsw-alias-border-l1, #dce2e9)', borderRadius: '10px', padding: '10px 12px', margin: '6px 0', background: 'var(--dsw-alias-bg-l1, #fff)', fontSize: '13px', lineHeight: '1.6' },
      // A failure card is visually distinct from a source card, so a glance cannot confuse "this call failed"
      // with "this call found a source".
      cardFailure: { border: '1px solid var(--dsw-alias-border-error, #e0b4b4)', borderRadius: '10px', padding: '10px 12px', margin: '6px 0', background: 'var(--dsw-alias-bg-error, #fdf3f3)', fontSize: '13px', lineHeight: '1.6' },
      failureBadge: { fontSize: '11px', border: '1px solid var(--dsw-alias-border-error, #e0b4b4)', borderRadius: '4px', padding: '0 6px', color: 'var(--dsw-alias-text-error, #8a2c2c)' },
      head: { display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px' },
      title: { fontSize: '13px' },
      badge: { fontSize: '11px', border: '1px solid var(--dsw-alias-border-l1, #dce2e9)', borderRadius: '4px', padding: '0 6px', opacity: 0.8 },
      field: { display: 'flex', gap: '6px', margin: '2px 0' },
      label: { minWidth: '64px', opacity: 0.65 },
      value: { flex: 1, wordBreak: 'break-word' },
      statusRow: { display: 'flex', flexWrap: 'wrap', gap: '10px', margin: '6px 0' },
      axisIdentity: { opacity: 0.85 },
      axisOk: { color: 'var(--dsw-alias-text-success, #245a4f)' },
      axisUnknown: { opacity: 0.7 },
      quote: { margin: '6px 0 6px 0', paddingLeft: '10px', borderLeft: '3px solid var(--dsw-alias-border-l2, #008c91)', whiteSpace: 'pre-wrap', wordBreak: 'break-word' },
      warning: { margin: '6px 0', padding: '6px 8px', borderRadius: '6px', background: 'var(--dsw-alias-bg-warning, #fff4d8)' },
      actions: { display: 'flex', flexWrap: 'wrap', gap: '8px', marginTop: '8px' },
      button: { font: 'inherit', cursor: 'pointer', border: '1px solid var(--dsw-alias-border-l1, #c9d3df)', borderRadius: '6px', background: 'transparent', color: 'inherit', padding: '3px 10px' },
      buttonDisabled: { font: 'inherit', cursor: 'not-allowed', border: '1px solid var(--dsw-alias-border-l1, #c9d3df)', borderRadius: '6px', background: 'transparent', color: 'inherit', padding: '3px 10px', opacity: 0.5 },
      panel: { marginTop: '8px', padding: '8px 10px', borderTop: '1px dashed var(--dsw-alias-border-l1, #dce2e9)' },
      pre: { whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: '12px', margin: '6px 0', padding: '6px', background: 'var(--dsw-alias-markdown-code-block, #f5f6f8)', borderRadius: '6px' },
      sub: { opacity: 0.72, margin: '2px 0' },
      candidate: { padding: '6px 0', borderBottom: '1px dashed var(--dsw-alias-border-l1, #dce2e9)' },
      candidateHead: { fontWeight: 600, wordBreak: 'break-word' },
    };

    /**
     * Register the card for each knowledge tool.
     *
     * `slots.inject(<slot>, cb)` (not a bare `register`) is the shipped idiom: it waits for the slot the
     * Tool layer declares, then registers this package's keyed entry into it. Keyed dispatch is by wire
     * tool name, so a name this build does not know simply falls through to the generic tool row.
     */
    function apply(ctx) {
      if (typeof ctx.inject === 'function') ctx.inject(['sidebarRightTabs', 'sidebarRight'], child => {
        const store = createPanelStore();
        const runtime = { store, trees: createPanelStore(), sidebar: child.sidebarRight, autoOpenMode:'manual' };
        child.effect(() => {
          const unregister = child.sidebarRightTabs.register({
            id: PANEL_KIND, kind: PANEL_KIND, title: () => '知识库',
            guide: [{ id: 'sources', order: 80, title: () => '知识库',
              description: () => '服务状态、调用上限与自动来源树' }],
          });
          const offClose=typeof runtime.sidebar.registerCloseHandler==='function'?runtime.sidebar.registerCloseHandler(PANEL_KIND,(id)=>{if(runtime.currentTurn?.id===id)runtime.closedTurns?.add(runtime.currentTurn.key);}):()=>{};
          panelRuntime = runtime; notifyRuntime();
          return () => { offClose(); unregister(); store.clear(); runtime.trees.clear(); if (panelRuntime === runtime) {panelRuntime = null;notifyRuntime();} };
        });
        if (typeof child.inject === 'function') child.inject(['sessions'], sessionCtx => {
          sessionCtx.effect(() => followSession(runtime, sessionCtx.sessions));
        });
        child.slots.inject('conversation.chat.assistant-actions', function*(){yield child.slots.register({name:'conversation.chat.assistant-actions',id:'knowledge-answer-sources',order:85,inject:sessionId=>({sessionId})},AnswerActions);});
        child.slots.inject('sidebar.right.pane.tab', function* () {
          yield child.slots.register({ name: 'sidebar.right.pane.tab', key: PANEL_KIND }, SourcePanel);
        });
      });
      ctx.slots.inject('tool.call.toolview', function* () {
        for (const name of TOOL_NAMES) {
          yield ctx.slots.register({ name: 'tool.call.toolview', key: name }, KnowledgeSourceCard);
        }
      });
    }

    // The one service this half needs. Declared so the entry activates only once the slot registry
    // exists rather than racing it at boot.
    const inject = ['slots'];

    exports.apply = apply;
    exports.inject = inject;
    return module.exports;
  },
});
