import { readFile, access } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import assert from 'node:assert/strict';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
for (const [file, language] of [['index.html', 'zh-CN'], ['en/index.html', 'en']]) {
  const path = resolve(root, 'dist', file);
  const html = await readFile(path, 'utf8');
  assert.ok(html.includes(`<html lang="${language}">`), `Incorrect language: ${file}`);
  assert.ok(!/\{\{[\w.]+\}\}/.test(html), `Unresolved template: ${file}`);
  assert.match(html, /<meta name="description" content="[^"]+">/);
  assert.match(html, /hreflang="x-default"/);
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
  assert.equal(new Set(ids).size, ids.length, `Duplicate IDs: ${file}`);
  for (const match of html.matchAll(/\b(?:href|src)="([^"]+)"/g)) {
    const target = match[1];
    if (target.startsWith('#')) {
      assert.ok(ids.includes(target.slice(1)), `Missing anchor ${target} in ${file}`);
    } else if (!/^https?:/.test(target)) {
      await access(resolve(dirname(path), target));
    }
  }
  for (const match of html.matchAll(/<a\b[^>]*target="_blank"[^>]*>/g)) {
    const relation = match[0].match(/rel="([^"]*)"/)?.[1].split(/\s+/) || [];
    assert.ok(relation.includes('noopener') && relation.includes('noreferrer'), `Unsafe external link: ${file}`);
  }
  const translations = html.match(/<script id="site-copy" type="application\/json">([\s\S]*?)<\/script>/);
  assert.ok(translations, `Missing interactive translations: ${file}`);
  JSON.parse(translations[1]);
  const fragmentUrl = html.match(/data-friends-src="([^"]+)"/)?.[1];
  assert.ok(fragmentUrl, `Missing friend fragment reference: ${file}`);
  if (!/^https?:/.test(fragmentUrl)) await access(resolve(dirname(path), fragmentUrl));
  assert.ok(!html.includes('https://aihub.doyix.com/'), `Friend links must not be inlined: ${file}`);
  console.log(`Checked ${file}: language, metadata, IDs, links, assets and interactive copy`);
}
