const API = '/v1/web';
let csrf = '';
let workspace = 'Nexus';

export function getWorkspace(){ return workspace; }
export function getCsrf(){ return csrf; }

async function json(url, options = {}) {
  const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', ...options,
    headers: { Accept: 'application/json', ...(options.headers || {}) } });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || body.message || `Request failed (${response.status})`);
  return body;
}

export async function bootstrap() {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const ticket = fragment.get('ticket');
  if (ticket) {
    const result = await json(`${API}/ticket/redeem`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ticket})});
    history.replaceState(null, '', location.pathname + location.search);
    csrf = result.csrf || '';
    workspace = result.workspace || workspace;
  }
  const result = await json(`${API}/bootstrap`);
  csrf = result.csrf || csrf;
  workspace = result.workspace || workspace;
  return result;
}

export async function command(command, {signal} = {}) {
  const result = await json(`${API}/command`, { method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':csrf}, body:JSON.stringify(command), signal });
  if (result.type === 'ErrorResult') throw new Error(result.message || 'The command failed');
  return result;
}

export async function voice(wav, requestId, {signal} = {}) {
  if (!(wav instanceof Blob) || wav.size < 44 || wav.size > 8 * 1024 * 1024) throw new Error('Voice recording is outside the allowed size');
  const result = await json(`${API}/voice?request_id=${encodeURIComponent(requestId)}`, {
    method: 'POST', headers: {'Content-Type':'audio/wav','X-CSRF-Token':csrf}, body: wav, signal,
  });
  if (result.type === 'ErrorResult') throw new Error(result.message || 'Voice transcription failed');
  return result;
}

export async function snapshot(session, signal) {
  return json(`${API}/session-view?session=${encodeURIComponent(session)}`, {signal});
}

export function eventUrl(session, seq) {
  return `${API}/session-events?session=${encodeURIComponent(session)}&from_seq=${seq}`;
}

export async function exportSession(session, format) {
  const result = await command({type:'SessionExport', session, format});
  return result;
}
