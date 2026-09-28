// THROWAWAY development server: in-memory UI exploration, never production settings.
import http from 'node:http';
import fs from 'node:fs';
const file=new URL('../src-python/chatgpt_web_settings.prototype.html',import.meta.url);
if(process.env.NODE_ENV==='production')throw new Error('The ChatGPT setup prototype is development-only.');
const port=Number(process.env.CODEXHUB_PROTOTYPE_PORT||43127);
http.createServer((req,res)=>{
  const assets={'/openai.svg':'../frontend/src/assets/providers/openai.svg','/codexhub.svg':'../frontend/src/assets/brand/codexhub-icon.svg'};
  const asset=assets[new URL(req.url,'http://localhost').pathname];
  if(req.method==='GET'&&asset){res.writeHead(200,{'Content-Type':'image/svg+xml'});res.end(fs.readFileSync(new URL(asset,import.meta.url)));return;}
  if(req.method!=='GET'||new URL(req.url,'http://localhost').pathname!=='/'){res.writeHead(404);res.end();return;}
  res.writeHead(200,{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store'});
  res.end(fs.readFileSync(file,'utf8').replace('__PROTOTYPE_DEV__','enabled'));
}).listen(port,'127.0.0.1',()=>console.log(`ChatGPT coding setup prototype: http://127.0.0.1:${port}/?variant=A\nVariants A / B / C; all actions are simulated. Ctrl+C stops the prototype.`));
