import { readFileSync } from 'node:fs';
import { Script } from 'node:vm';

const frontendPath = new URL('../data/智拓商机作战助手-开发版.html', import.meta.url);
const html = readFileSync(frontendPath, 'utf8');
const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/gi)]
  .map((match) => match[1])
  .filter(Boolean);

if (scripts.length === 0) {
  throw new Error('Frontend has no inline JavaScript to validate.');
}
for (const script of scripts) {
  new Script(script, { filename: '智拓商机作战助手-开发版.html' });
}
if (!html.includes('location.pathname.indexOf("/app/")===0?location.origin')) {
  throw new Error('Frontend API must use the same origin when served under /app/.');
}
console.log(`Frontend syntax and API origin checks passed (${scripts.length} script block).`);
