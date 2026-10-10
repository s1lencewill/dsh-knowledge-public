"""Human-only local staging. Originals and published versions are immutable to this importer."""
import copy
import json
import os
from pathlib import Path
import shutil
import threading
import time
import uuid
import capacity
import catalog
import evidence
import markdown_library
import source_card

PDF_INPUT_LIMIT = 32_000_000
class Refused(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def checked_root(root, workspace):
    raw, space = Path(root), Path(workspace)
    if not raw.is_absolute() or not space.is_absolute(): raise Refused('FOLDER_INVALID')
    try:
        if any(evidence.linked(p) for p in [raw, *raw.parents]): raise Refused('LINK_NOT_ALLOWED')
        actual, space = raw.resolve(strict=True), space.resolve(strict=True)
        if not actual.is_dir() or not space.is_dir() or not actual.is_relative_to(space): raise Refused('FOLDER_OUTSIDE_WORKSPACE')
        if space.parent == space: raise Refused('WORKSPACE_TOO_BROAD')
        return actual, space
    except OSError: raise Refused('FOLDER_INVALID') from None

def source_files(root):
    files, excluded, problems = [], [], []
    for folder, dirs, names in os.walk(root, followlinks=False, onerror=lambda _: problems.append({'reason':'UNREADABLE_DIRECTORY'})):
        parent = Path(folder)
        retained = []
        for name in dirs:
            path = parent / name
            if evidence.linked(path) or evidence.excluded(name): excluded.append({'path':path.relative_to(root).as_posix(),'reason':'LINK_OR_HIDDEN'})
            else: retained.append(name)
        dirs[:] = retained
        for name in sorted(names):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if evidence.linked(path) or evidence.excluded(name): excluded.append({'path':relative,'reason':'LINK_OR_HIDDEN'})
            elif path.suffix.lower() not in {'.md','.pdf'}: excluded.append({'path':relative,'reason':'UNSUPPORTED_TYPE'})
            else: files.append((relative,path))
    return sorted(files),excluded,problems

def scan(root, limit=capacity.DEFAULT_MAX_FILE_BYTES, destination=None):
    files, excluded, problems = source_files(root)
    if len(files)>capacity.MAX_FILES: raise Refused('LIBRARY_CAPACITY_EXCEEDED')
    if sum(p.stat().st_size for _,p in files)>capacity.MAX_TOTAL_BYTES:
        problems.append({'reason':'SOURCE_BYTES_EXCEEDED','limit':capacity.MAX_TOTAL_BYTES})
        return {'records':{},'manifest':{},'sourceManifest':{},'excluded':excluded,'problems':problems,'quality':[],
            'documents':0,'sourceFiles':len(files),'totalBytes':0,'markdown':sum(p.suffix.lower()=='.md' for _,p in files),'pdf':sum(p.suffix.lower()=='.pdf' for _,p in files)}
    records, manifest, origins, quality = {},{},{},[]
    total=0
    for relative,path in files:
        try:
            maximum = limit if path.suffix.lower()=='.md' else PDF_INPUT_LIMIT
            if path.stat().st_size>maximum:
                problems.append({'path':relative,'reason':'FILE_TOO_LARGE','limit':maximum});continue
            data=path.read_bytes()
            if len(data)>maximum: raise Refused('SOURCE_CHANGED')
            origins[relative]=evidence.digest(data)
            key=evidence.digest(relative.encode())[:24]
            output=key+('.md' if path.suffix.lower()=='.md' else '.txt')
            if path.suffix.lower()=='.md':
                text=data.decode('utf-8-sig')
                front,note,truncated=markdown_library.front_matter(text)
                record=markdown_library.note_record(output,data,front,truncated,note)
                record['fileName']=path.name
                if not front.get('title'):
                    record['titleRaw']=path.stem;record['titleNormalized']=catalog.squash(path.stem)
            else:
                import pdf_extract
                text,_,_,audit=pdf_extract.render(path,key)
                if audit['pdfSHA256']!=origins[relative] or evidence.digest(path.read_bytes())!=origins[relative]: raise Refused('SOURCE_CHANGED')
                data=text.encode('utf-8')
                if sum(p['nonWhitespace'] for p in audit['pages'])==0:
                    excluded.append({'path':relative,'reason':'SCANNED_PDF_NO_OCR'});continue
                record=markdown_library.note_record(output,data,{},note='PDF title not verified')
                record.update(fileName=path.name,titleRaw=path.stem,titleNormalized=catalog.squash(path.stem),
                    pages=len(audit['pages']),pdfSHA256=origins[relative],pageMap=audit['pages'],
                    pageMapSource={'mappingVersion':source_card.PAGE_MAP_VERSION,'source':'extraction-audit','auditTextSHA256':audit['textSHA256'],'pdfSHA256':audit['pdfSHA256']},
                    quality={'replacementChars':audit['replacementChars'],'privateUseChars':audit['privateUseChars'],
                        'reviewPages':sorted({v['page'] for v in audit['flags'] if v['flag'] in {'character-loss','layout-ambiguous','extract-error'}})})
                quality.append({'path':relative,'pages':len(audit['pages']),'flags':sorted({v['flag'] for v in audit['flags']}),'limitations':'NO_OCR_FORMULAS_TABLES_REQUIRE_REVIEW'})
                if destination:
                    (destination.parent/'audit').mkdir(exist_ok=True)
                    (destination.parent/'audit'/(key+'.json')).write_text(json.dumps(audit),encoding='utf-8')
            if len(data)>limit: problems.append({'path':relative,'reason':'EXTRACTED_TEXT_TOO_LARGE','limit':limit});continue
            total+=len(data)
            record['sourceFile']=relative
            records[key]=record;manifest[output]=evidence.digest(data)
            if destination: (destination/output).write_bytes(data)
        except Refused: raise
        except UnicodeError: problems.append({'path':relative,'reason':'NOT_UTF8'})
        except Exception: problems.append({'path':relative,'reason':'EXTRACTION_OR_READ_FAILED'})
    if total>capacity.MAX_TOTAL_BYTES: problems.append({'reason':'TOTAL_BYTES_EXCEEDED'})
    if not records: problems.append({'reason':'NO_READABLE_DOCUMENTS'})
    return {'records':records,'manifest':manifest,'sourceManifest':origins,'excluded':excluded,'problems':problems,'quality':quality,
        'documents':len(records),'sourceFiles':len(files),'totalBytes':total,
        'markdown':sum(p.suffix.lower()=='.md' for _,p in files),'pdf':sum(p.suffix.lower()=='.pdf' for _,p in files)}

def original_manifest(root):
    files,_,problems=source_files(Path(root))
    if problems: raise Refused('SOURCE_CHANGED')
    result={}
    for relative,path in files:
        if path.stat().st_size>PDF_INPUT_LIMIT: raise Refused('SOURCE_CHANGED')
        result[relative]=evidence.digest(path.read_bytes())
    return result

class Manager:
    def __init__(self, config, clock=time.monotonic):
        self.config,self.clock=Path(config),clock
        self.previews,self.lock={},threading.Lock()
    def load(self):
        raw=self.config.read_bytes();value=json.loads(raw.decode('utf-8-sig'))
        if not isinstance(value,dict) or not isinstance(value.get('libraries'),list): raise Refused('CONFIG_INVALID')
        return raw,value
    def preview(self,workspace,session,root=None,library_id=None,name=None):
        raw,config=self.load();old=None
        if library_id:
            old=next((v for v in config['libraries'] if v['id']==library_id),None)
            if not old or not old.get('sourceRoot'): raise Refused('UPDATE_UNAVAILABLE')
            root,name=old['sourceRoot'],old['name']
            # An update cannot migrate the library to the currently opened broader workspace.
            if Path(workspace).resolve()!=Path(old['workspace']).resolve(): raise Refused('UPDATE_WORKSPACE_MISMATCH')
        root,workspace=checked_root(root,workspace)
        if self.config.parent.resolve().is_relative_to(root): raise Refused('FOLDER_OVERLAPS_STORAGE')
        result=scan(root,capacity.resolve(old or {}))
        before=(old or {}).get('sourceManifest',{});after=result['sourceManifest']
        diff={'added':sorted(set(after)-set(before)),'removed':sorted(set(before)-set(after)),
            'changed':sorted(k for k in set(before)&set(after) if before[k]!=after[k])}
        token='kbp-'+uuid.uuid4().hex
        entry={'workspace':str(workspace),'session':session,'root':str(root),'old':copy.deepcopy(old),'name':name or root.name,
            'result':{k:v for k,v in result.items() if k!='records'},'configHash':evidence.digest(raw),'at':self.clock()}
        with self.lock:
            self.previews={k:v for k,v in self.previews.items() if self.clock()-v['at']<900}
            if len(self.previews)>=20: raise Refused('PREVIEW_LIMIT')
            self.previews[token]=entry
        return {'previewId':token,**{k:v for k,v in result.items() if k not in {'records','manifest','sourceManifest'}},
            'diff':diff,'canPublish':not result['problems'],'workspace':str(workspace),'root':str(root),'name':entry['name'],
            'externalData':'ENGINE_SNIPPETS_AND_AGENT_READ_CONTEXT','maxFileBytes':capacity.resolve(old or {})}
    def cancel(self,token,workspace,session):
        workspace=str(Path(workspace).resolve(strict=True))
        with self.lock:
            entry=self.previews.get(token)
            if entry and (entry['workspace'],entry['session'])==(workspace,session): self.previews.pop(token)
        return {'code':'PREVIEW_CANCELLED'}
    def publish(self,token,workspace,session,consent):
        if consent is not True: raise Refused('CONSENT_REQUIRED')
        workspace=str(Path(workspace).resolve(strict=True))
        with self.lock:
            entry=self.previews.get(token)
            if not entry or self.clock()-entry['at']>=900: raise Refused('PREVIEW_EXPIRED')
            if (entry['workspace'],entry['session'])!=(workspace,session): raise Refused('PREVIEW_FOREIGN')
            if entry['result']['problems']: raise Refused('PREFLIGHT_FAILED')
            guard=self.config.with_name(self.config.name+'.publish-lock')
            try: fd=os.open(guard,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            except FileExistsError: raise Refused('PUBLISH_BUSY') from None
            version=None
            try:
                raw,config=self.load()
                if evidence.digest(raw)!=entry['configHash']: raise Refused('CONFIG_CHANGED')
                root,_=checked_root(entry['root'],workspace)
                if original_manifest(root)!=entry['result']['sourceManifest']: raise Refused('SOURCE_CHANGED')
                library_id=(entry['old'] or {}).get('id') or ('library-'+uuid.uuid4().hex[:12])
                version=self.config.parent/'managed-libraries'/library_id/uuid.uuid4().hex
                texts=version/'texts';texts.mkdir(parents=True)
                built=scan(root,capacity.resolve(entry['old'] or {}),texts)
                if built['problems'] or built['manifest']!=entry['result']['manifest'] or original_manifest(root)!=built['sourceManifest']: raise Refused('SOURCE_CHANGED')
                payload=catalog.binding_fields(library_id,texts);payload['documents']=built['records']
                catalogue=version/'catalogue.json';catalogue.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
                catalog.load_bound(catalogue,library_id,texts,require_attribution=True)
                lib={**(entry['old'] or {}),'id':library_id,'name':entry['name'],'workspace':workspace,'root':str(texts),
                    'sourceRoot':str(root),'sourceManifest':built['sourceManifest'],'catalogue':str(catalogue),'syntheticFixture':False,
                    'realDataTrial':{'authorized':True,'files':built['manifest']}}
                if evidence.digest(self.config.read_bytes())!=entry['configHash']: raise Refused('CONFIG_CHANGED')
                config['libraries']=[v for v in config['libraries'] if v['id']!=library_id]+[lib]
                backup=self.config.parent/'backups'/('onboarding-'+uuid.uuid4().hex);backup.mkdir(parents=True)
                (backup/self.config.name).write_bytes(raw)
                temporary=self.config.with_name(self.config.name+'.'+uuid.uuid4().hex+'.tmp')
                try:
                    temporary.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(temporary,self.config)
                finally: temporary.unlink(missing_ok=True)
                self.previews.pop(token)
                return {'code':'LIBRARY_PUBLISHED','libraryId':library_id,'documents':built['documents']}
            except BaseException:
                if version: shutil.rmtree(version,ignore_errors=True)
                raise
            finally:
                os.close(fd);guard.unlink(missing_ok=True)

    def unregister(self, library_id, workspace, consent):
        if consent is not True: raise Refused('CONSENT_REQUIRED')
        with self.lock:
            guard=self.config.with_name(self.config.name+'.publish-lock')
            try: fd=os.open(guard,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            except FileExistsError: raise Refused('PUBLISH_BUSY') from None
            try:
                raw,config=self.load()
                lib=next((v for v in config['libraries'] if v['id']==library_id),None)
                if lib is None: raise Refused('LIBRARY_NOT_VISIBLE')
                visible=Path(lib.get('sourceRoot') or lib['root']).resolve(strict=True)
                space=Path(workspace).resolve(strict=True)
                if not visible.is_relative_to(space) or not space.is_relative_to(Path(lib['workspace']).resolve()): raise Refused('LIBRARY_NOT_VISIBLE')
                config['libraries']=[v for v in config['libraries'] if v['id']!=library_id]
                backup=self.config.parent/'backups'/('unregister-'+uuid.uuid4().hex);backup.mkdir(parents=True)
                (backup/self.config.name).write_bytes(raw)
                temporary=self.config.with_name(self.config.name+'.'+uuid.uuid4().hex+'.tmp')
                try:
                    temporary.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(temporary,self.config)
                finally: temporary.unlink(missing_ok=True)
                return {'code':'LIBRARY_UNREGISTERED','originalsDeleted':False,'versionsDeleted':False}
            finally:
                os.close(fd);guard.unlink(missing_ok=True)
