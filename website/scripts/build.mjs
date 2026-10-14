import { readFile, writeFile, mkdir, copyFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { copy } from '../src/content.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const template = await readFile(resolve(root, 'src/template.html'), 'utf8');
const sharedFragmentUrl = process.env.FRIENDS_HTML_URL || '';
if (sharedFragmentUrl) {
  const url = new URL(sharedFragmentUrl);
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) {
    throw new Error('FRIENDS_HTML_URL must be an HTTP(S) fragment URL without credentials');
  }
}
if (JSON.stringify(Object.keys(copy.zh).sort()) !== JSON.stringify(Object.keys(copy.en).sort())) {
  throw new Error('Chinese and English translation keys do not match');
}
const escape = value => String(value).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
await mkdir(resolve(root, 'dist/assets'), { recursive: true });
await mkdir(resolve(root, 'dist/en'), { recursive: true });
for (const asset of ['site.css', 'site.js', 'favicon.svg']) {
  await copyFile(resolve(root, 'src', asset), resolve(root, 'dist/assets', asset));
}
if (!sharedFragmentUrl) {
  const sharedFile = process.env.FRIENDS_HTML_PATH;
  const fragment = sharedFile ? await readFile(resolve(sharedFile), 'utf8') :
    await readFile(resolve(root, '../../common_footer/friends-static.html'), 'utf8').catch(error => {
      if (error.code !== 'ENOENT') throw error;
      return readFile(resolve(root, 'src/friends-static.html'), 'utf8');
    });
  await writeFile(resolve(root, 'dist/assets/friends-static.html'), fragment);
}
for (const [language, content] of Object.entries(copy)) {
  const values = {
    ...content, language: language === 'zh' ? 'zh-CN' : 'en',
    locale: language === 'zh' ? 'zh_CN' : 'en_US',
    base: language === 'zh' ? './' : '../',
    alternate: language === 'zh' ? './en/index.html' : '../index.html',
    alternateLang: language === 'zh' ? 'en' : 'zh-CN',
    friendsUrl: sharedFragmentUrl || (language === 'zh' ? './' : '../') + 'assets/friends-static.html',
    translations: JSON.stringify(content).replace(/</g, '\\u003c')
  };
  const html = template.replace(/\{\{([\w.]+)\}\}/g, (_, key) => {
    if (!(key in values)) throw new Error(`Missing ${language} translation: ${key}`);
    return key === 'translations' ? values[key] : escape(values[key]);
  });
  await writeFile(resolve(root, language === 'zh' ? 'dist/index.html' : 'dist/en/index.html'), html);
}
await writeFile(resolve(root, 'dist/.nojekyll'), '');
console.log(`Built Chinese and English static pages: ${resolve(root, 'dist')}`);
