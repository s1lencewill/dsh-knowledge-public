import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdirSync,mkdtempSync,readFileSync,realpathSync,rmSync,writeFileSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {writeProfile} from '../trial/lib.mjs';
const name='@s1lencewill/dsh-knowledge';
function fixture(t,packageName=name){
 const base=resolve('.local/scoped-profile-tests');mkdirSync(base,{recursive:true});const root=mkdtempSync(join(base,'case-'));
 t.after(()=>{assert.equal(resolve(root,'..'),base);rmSync(root,{recursive:true,force:true});});
 const paths={app:join(root,'app'),profilesDir:join(root,'profiles'),token:join(root,'token')};mkdirSync(paths.app);
 writeFileSync(join(paths.app,'package.json'),JSON.stringify({name:packageName}));writeFileSync(paths.token,'synthetic-token');
 const config={profileName:'synthetic',picker:'browse',servicePort:19090,modelProvider:'synthetic',titleModel:'synthetic',modelApiKeyEnv:'SYNTHETIC_KEY'};
 return {root,paths,config,profile:join(paths.profilesDir,'synthetic')};
}
test('scoped package composes its own bundle and resolves from the profile',t=>{
 const {paths,config,profile}=fixture(t);assert.equal(writeProfile(paths,config).ok,true);
 const pkg=JSON.parse(readFileSync(join(profile,'package.json'),'utf8'));
 assert.ok(pkg.dsh.profile.bundles.includes(name));assert.equal(pkg.dependencies[name],`link:${paths.app.replaceAll('\\','/')}`);
 assert.equal(realpathSync(join(profile,'node_modules',name)),realpathSync(paths.app));
});
test('upgrade replaces the legacy bundle instead of registering both',t=>{
 const {paths,config,profile}=fixture(t);mkdirSync(profile,{recursive:true});writeFileSync(join(profile,'package.json'),JSON.stringify({dependencies:{'dsh-knowledge':'link:legacy'},dsh:{profile:{bundles:['dsh-knowledge','other-plugin']}}}));
 assert.equal(writeProfile(paths,config).ok,true);const pkg=JSON.parse(readFileSync(join(profile,'package.json'),'utf8'));
 assert.deepEqual(pkg.dsh.profile.bundles,['other-plugin',name]);assert.equal(pkg.dependencies['dsh-knowledge'],undefined);
});
test('reinstallation does not duplicate the scoped bundle',t=>{
 const {paths,config,profile}=fixture(t);writeProfile(paths,config);writeProfile(paths,config);
 assert.equal(JSON.parse(readFileSync(join(profile,'package.json'),'utf8')).dsh.profile.bundles.filter(x=>x===name).length,1);
});
test('a package identity cannot turn into a parent-directory link',t=>{
 const {paths,config}=fixture(t,'../outside');assert.throws(()=>writeProfile(paths,config),/INVALID_PACKAGE_NAME/);
});
test('first installation reads the staged identity while the future app path does not exist',t=>{
 const f=fixture(t);const staged=join(f.root,'app.new');mkdirSync(staged,{recursive:true});writeFileSync(join(staged,'package.json'),JSON.stringify({name}));
 assert.equal(resolve(f.paths.app,'..'),resolve(f.root));rmSync(f.paths.app,{recursive:true,force:true});const result=writeProfile(f.paths,f.config,staged);assert.equal(result.ok,true);
 const pkg=JSON.parse(readFileSync(join(f.paths.profilesDir,f.config.profileName,'package.json'),'utf8'));assert.ok(pkg.dsh.profile.bundles.includes(name));
});
