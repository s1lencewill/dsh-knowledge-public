import { existsSync, mkdirSync, symlinkSync } from 'node:fs';
import { join, resolve } from 'node:path';
const globalRoot = process.env.DSH_HOST_NODE_MODULES ?? join(process.env.APPDATA ?? '', 'npm/node_modules/@deepseek-ai/dsh/node_modules');
const target = join(globalRoot, '@deepseek-ai');
if (!existsSync(join(target, 'dsh-tools/package.json'))) throw new Error('Set DSH_HOST_NODE_MODULES to the installed DSH node_modules directory');
mkdirSync('node_modules', { recursive: true });
const link = resolve('node_modules/@deepseek-ai');
if (!existsSync(link)) symlinkSync(target, link, process.platform === 'win32' ? 'junction' : 'dir');
console.log('Host SDK linked locally (not distributed).');
