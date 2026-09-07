// THROWAWAY fixtures: deterministic local data for the desktop design prototype.
import type { GatewayUsageEvent, Provider } from '../lib/types';
import openai from './prototype-assets/openai.svg';
import anthropic from './prototype-assets/anthropic.svg';
import deepseek from './prototype-assets/deepseek.svg';
import kimi from '../assets/providers/kimi.svg';
import xai from '../assets/providers/xai.svg';
import codex from '../assets/codex-logo.svg';
import opencode from '../assets/opencode-icon.png';
import pi from '../assets/pi-icon.png';
import zcode from '../assets/zcode-icon.png';
import dsh from '../assets/dsh-icon.svg';
import omp from '../assets/omp-icon.png';
export const providerFixtures = [
  { id:'openai', name:'OpenAI', icon:openai, kind:'Codex 订阅', value:'72', unit:'%', caption:'5 小时窗口剩余', percent:72, note:'2 小时 18 分后重置', models:8, model:'gpt-5.4', color:'#8170dd', base:'https://api.openai.com/v1' },
  { id:'xai', name:'xAI', icon:xai, kind:'Grok 订阅', value:'68', unit:'%', caption:'每周剩余', percent:68, note:'4 天后重置', models:1, model:'grok-4', color:'#8d829e', base:'https://api.x.ai/v1' },
  { id:'anthropic', name:'Anthropic', icon:anthropic, kind:'API 余额', value:'48.60', unit:'$', caption:'可用余额', percent:0, note:'今日使用 $3.24', models:5, model:'claude-sonnet-4-6', color:'#d39575', base:'https://api.anthropic.com' },
  { id:'deepseek', name:'DeepSeek', icon:deepseek, kind:'API 余额', value:'126.80', unit:'¥', caption:'可用余额', percent:0, note:'今日使用 ¥2.18', models:2, model:'deepseek-chat', color:'#5a92ce', base:'https://api.deepseek.com/v1' },
  { id:'kimi', name:'Kimi', icon:kimi, kind:'Token 配额', value:'12', unit:'%', caption:'剩余 1.2M Token', percent:12, note:'额度偏低', models:3, model:'kimi-k2.5', color:'#b99b68', base:'https://api.moonshot.cn/v1' },
];
export const clientFixtures = [
  {id:'opencode',name:'OpenCode',icon:opencode,kind:'终端客户端',path:'~/.config/opencode/opencode.json',on:true},
  {id:'dsh',name:'DeepSeek Harness',icon:dsh,kind:'Agent 运行时',path:'~/.dsh/settings.yaml',on:true},
  {id:'pi',name:'Pi',icon:pi,kind:'轻量 CLI',path:'~/.pi/agent/config.json',on:false},
  {id:'zcode',name:'ZCode',icon:zcode,kind:'IDE 扩展',path:'~/.zcode/v2/config.json',on:true},
  {id:'omp',name:'OMP',icon:omp,kind:'提示词运行时',path:'~/.omp/agent/config.yml',on:false},
];
export const codexIcon=codex;
export const chartProviders:Provider[]=providerFixtures.map(p=>({id:p.id,name:p.name,base_url:p.base,enabled:true,models:[],reports_cached_input_tokens:true}));
export function makeEvents():GatewayUsageEvent[]{
  const events:GatewayUsageEvent[]=[];
  for(let day=0;day<62;day++) for(let p=0;p<providerFixtures.length;p++) for(let n=0;n<12+(day*7+p*11)%36;n++){
    const ts=new Date(); ts.setDate(ts.getDate()-day);ts.setHours(n%24,(n*7)%60,0,0);
    const input=1200+(day*331+n*523+p*918)%12000,output=300+(n*61+day*31)%1800;
    events.push({ts:ts.toISOString(),request_id:`demo-${day}-${p}-${n}`,model:providerFixtures[p].model,upstream:providerFixtures[p].id,client_id:['codex','opencode','dsh','pi'][n%4],status:n%29===0?502:200,duration_ms:480+n*17,usage_source:'upstream',reports_cached_input_tokens:true,input_tokens:input,output_tokens:output,total_tokens:input+output,cached_input_tokens:Math.round(input*.38)});
  }
  return events;
}
