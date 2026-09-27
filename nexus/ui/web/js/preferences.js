const valid=new Set(['focused','balanced','complete']);
const browserKey='nexus-web-detail-browser';
const workspaceKey=workspace=>`nexus-web-detail-workspace:${encodeURIComponent(workspace)}`;
const sessionKey=(workspace,session)=>`nexus-web-detail-session:${encodeURIComponent(workspace)}:${encodeURIComponent(session)}`;
function read(key){try{const value=localStorage.getItem(key);if(value===null)return null;if(valid.has(value))return value;localStorage.removeItem(key);}catch{}return null;}
function write(key,value){try{localStorage.setItem(key,value);}catch{}}
function remove(key){try{localStorage.removeItem(key);}catch{}}
export function resolveDetail(workspace,session){
  const scoped=session?read(sessionKey(workspace,session)):null;if(scoped)return {value:scoped,source:'Session override'};
  const local=read(workspaceKey(workspace));if(local)return {value:local,source:'Workspace default'};
  return {value:read(browserKey)||'balanced',source:'Browser default'};
}
export function getSessionOverride(workspace,session){return session?read(sessionKey(workspace,session)):null;}
export function getWorkspaceDefault(workspace){return read(workspaceKey(workspace));}
export function getBrowserDefault(){return read(browserKey);}
export function setSessionDetail(workspace,session,value){if(session&&valid.has(value))write(sessionKey(workspace,session),value);}
export function clearSessionDetail(workspace,session){if(session)remove(sessionKey(workspace,session));}
export function setWorkspaceDetail(workspace,value){if(valid.has(value))write(workspaceKey(workspace),value);}
export function clearWorkspaceDetail(workspace){remove(workspaceKey(workspace));}
export function setBrowserDetail(value){if(valid.has(value))write(browserKey,value);}
export function clearBrowserDetail(){remove(browserKey);}
export function getTheme(){try{const v=localStorage.getItem('nexus-web-theme');return ['system','light','dark'].includes(v)?v:'dark';}catch{return 'dark';}}
export function setTheme(v){if(['system','light','dark'].includes(v))write('nexus-web-theme',v);}
