import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
import ts from 'typescript';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const output=path.join(root,'.runtime','charting-tests');
await fs.rm(output,{recursive:true,force:true});
await fs.mkdir(output,{recursive:true});
await fs.writeFile(path.join(output,'package.json'),'{"type":"module"}');
async function compile(dir){
 for(const item of await fs.readdir(path.join(root,dir),{withFileTypes:true})){
  const relative=path.join(dir,item.name);
  if(item.isDirectory()) await compile(relative);
  else if(item.name.endsWith('.ts')&&!item.name.endsWith('.d.ts')){
   let code=ts.transpileModule(await fs.readFile(path.join(root,relative),'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext,verbatimModuleSyntax:false}}).outputText;
   code=code.replace(/(from\s*['"]|import\s*['"])(\.[^'"]+)(['"])/g,(_,prefix,spec,suffix)=>prefix+(spec.endsWith('.ts')?spec.slice(0,-3)+'.js':path.extname(spec)?spec:spec+'.js')+suffix);
   const target=path.join(output,relative.replace(/\.ts$/,'.js'));await fs.mkdir(path.dirname(target),{recursive:true});await fs.writeFile(target,code);
  }
 }
}
await compile('src/charting');
await compile('src/pages');
await compile('tests');
await fs.mkdir(path.join(output,'src'),{recursive:true});
await fs.writeFile(path.join(output,'src/types.js'),'');
const tests=(await fs.readdir(path.join(output,'tests'))).filter(x=>x.endsWith('.test.js')).map(x=>path.join(output,'tests',x));
const nativeTests=(await fs.readdir(path.join(root,'tests'))).filter(x=>x.endsWith('.test.mjs')).map(x=>path.join(root,'tests',x));
if(!tests.length&&!nativeTests.length)throw Error('No charting tests found');
const result=spawnSync(process.execPath,['--test',...tests,...nativeTests],{cwd:root,stdio:'inherit'});
process.exit(result.status??1);
