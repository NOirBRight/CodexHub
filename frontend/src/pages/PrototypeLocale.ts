// THROWAWAY presentation-only translations. State identifiers and entered text stay unchanged.
import i18n, { localeResources } from '../i18n';
import translations from './PrototypeLocale.json';
const inherited:Record<string,string>={};
function collect(zh:Record<string,unknown>,en:Record<string,unknown>){for(const [k,v] of Object.entries(zh)){if(typeof v==='string'&&typeof en[k]==='string')inherited[v]=en[k] as string;else if(v&&typeof v==='object'&&en[k]&&typeof en[k]==='object')collect(v as Record<string,unknown>,en[k] as Record<string,unknown>);}}
collect(localeResources['zh-CN'],localeResources['en-US']);
const dictionary:Record<string,string>={...inherited,...translations};
export function pt<T>(value:T):T{
  if(typeof value!=='string'||!i18n.language?.startsWith('en'))return value;
  const trimmed=value.trim();
  if(dictionary[trimmed])return value.replace(trimmed,dictionary[trimmed]) as T;
  const patterns:Array<[RegExp,(...parts:string[])=>string]>=[
    [/^(.*) 图标$/,(_,name)=>name+' icon'],
    [/^(.*) 资源详情$/,(_,name)=>name+' resources'],
    [/^(.*) 详情$/,(_,name)=>name+' details'],
    [/^启用 (.*)$/,(_,name)=>'Enable '+name],
    [/^上移 (.*)$/,(_,name)=>'Move '+name+' up'],
    [/^下移 (.*)$/,(_,name)=>'Move '+name+' down'],
    [/^(连接|断开) (.*)$/,(_,action,name)=>(action==='连接'?'Connect ':'Disconnect ')+name],
    [/^(\d+) 个预设模型$/,(_,n)=>n+' preset models'],
    [/^(\d+) 个 Gateway 模型可见$/,(_,n)=>n+' Gateway models available'],
    [/^将导入 (\d+) 个预设模型；添加后可发现、编辑和测试模型。$/,(_,n)=>'Import '+n+' preset models. Discover, edit and test after adding.'],
    [/^(.*) 连接配置已更新 · 请重启 (.*)$/,(_,name,target)=>name+' connection updated · restart '+target],
    [/^(.*) 版本已检查 · 无需重启$/,(_,name)=>name+' version checked · no restart required'],
    [/^(.*) 已(启用|停用)$/,(_,name,state)=>name+(state==='启用'?' enabled':' disabled')]
  ];
  for(const [pattern,format] of patterns){if(pattern.test(value))return value.replace(pattern,(...args)=>format(...args.slice(0,-2))) as T;}
  if(value.includes(' · '))return value.split(' · ').map(part=>dictionary[part]||part).join(' · ') as T;
  return value as T;
}
