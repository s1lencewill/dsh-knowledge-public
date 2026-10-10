"""Cold source build of rg/rga for the manual Windows release-validation job."""
import argparse,hashlib,json,os,runpy,shutil,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
RG_COMMIT='af60c2de9d85e7f3d81c78601669468cf02dabab'
def run(args,**kw):
 return subprocess.check_output(args,text=True,encoding='utf8',errors='replace',**kw).strip()
def main():
 p=argparse.ArgumentParser();p.add_argument('--out',required=True);a=p.parse_args()
 if os.name!='nt':raise SystemExit('WINDOWS_REQUIRED')
 builder=runpy.run_path(str(ROOT/'scripts/build-rga-patched.py'))
 vc=builder['find_vcvars']()
 if vc is None or not vc.is_file():raise SystemExit('MSVC_REQUIRED')
 source=ROOT/'.local/ci/rg-source';rg=ROOT/'.local/tools/rg.exe';rga=ROOT/'.local/tools/rga-patched/rga.exe'
 if source.exists() or rg.exists() or rga.exists() or (ROOT/'.local/rga-src').exists():raise SystemExit('COLD_BUILD_REQUIRES_EMPTY_TOOL_DIRECTORIES')
 source.parent.mkdir(parents=True,exist_ok=True)
 run(['git','clone','--depth','1','--branch','15.1.0','https://github.com/BurntSushi/ripgrep.git',str(source)])
 if run(['git','rev-parse','HEAD'],cwd=source)!=RG_COMMIT:raise SystemExit('RG_SOURCE_COMMIT_CHANGED')
 command='call "%s" >nul 2>&1 && cargo build --release --locked --features pcre2 --manifest-path "%s"' % (vc,source/'Cargo.toml')
 run(['cmd','/c',command]);rg.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source/'target/release/rg.exe',rg)
 run([sys.executable,str(ROOT/'scripts/build-rga-patched.py')])
 expected=json.loads((ROOT/'tools/rga/SOURCE.json').read_text(encoding='utf8'))
 if run(['git','rev-parse','HEAD'],cwd=ROOT/'.local/rga-src')!=expected['commit']:raise SystemExit('RGA_SOURCE_COMMIT_CHANGED')
 if hashlib.sha256((ROOT/'tools/rga/cache-path-fallback.patch').read_bytes()).hexdigest()!=expected['patchSHA256']:raise SystemExit('RGA_PATCH_CHANGED')
 os.environ['DSH_KNOWLEDGE_RG']=str(rg);os.environ['DSH_KNOWLEDGE_RGA']=str(rga)
 run([sys.executable,str(ROOT/'scripts/pin-tools.py')])
 notices=ROOT/'.local/tools/licenses';notices.mkdir()
 for name in ['LICENSE-MIT','UNLICENSE','COPYING']:shutil.copy2(source/name,notices/('rg-'+name))
 shutil.copy2(ROOT/'.local/rga-src/LICENSE.md',notices/'rga-AGPL.md')
 report={'schemaVersion':1,'coldToolBuild':True,'python':sys.version,'rustc':run(['rustc','-Vv']),'cargo':run(['cargo','-V']),
         'sources':{'rg':RG_COMMIT,'rga':expected['commit']},'tools':{name:{'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'version':run([str(path),'--version'])} for name,path in [('rg',rg),('rga',rga)]},
         'binaryDistributionAllowed':False,'cleanWindowsFullAcceptance':False}
 out=Path(a.out);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
 print('Cold tools built; runtime and installation gates remain separate.')
if __name__=='__main__':main()
