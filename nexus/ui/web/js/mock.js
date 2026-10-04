// ui_support/mock_args.py. Registered only when the daemon reports `dev: true`,
// so a normal daemon never shows the command.
export function parseMockArgs(args) {
  const out = {action: 'list', scenario: '', speed: 1, seed: 0, error: ''}, rest = [];
  for (let i = 0; i < args.length; i++) {
    const item = args[i];
    if (item === '--speed' || item === '--seed') {
      const value = Number(args[i + 1]);
      if (args[i + 1] === undefined || !Number.isFinite(value)) { out.error = `${item} needs a number`; return out; }
      if (item === '--speed') out.speed = Math.max(0, value); else out.seed = Math.trunc(value);
      i++;
    } else rest.push(item);
  }
  if (!rest.length || rest[0] === 'list') out.action = 'list';
  else if (rest[0] === 'clean') out.action = 'clean';
  else { out.action = 'run'; out.scenario = rest[0]; }
  return out;
}

export function formatScenarios(rows) {
  const width = Math.max(0, ...rows.map(r => r.name.length));
  return ['Mock scenarios (run with /mock NAME [--speed N]):', ...rows.map(r => {
    const flags = [r.interactive ? 'interactive' : '', r.slow ? 'slow' : ''].filter(Boolean).join(' ');
    return `  ${r.name.padEnd(width)}  ~${r.est_seconds}s  ${r.summary}${flags ? `  [${flags}]` : ''}`;
  })].join('\n');
}

export async function installMock({api, commands, notify, showText, openSession, refreshSessions}) {
  let health;
  try { health = await api.command({type: 'Health'}); } catch { return false; }
  if (!health || !health.dev) return false;
  const run = async args => {
    const parsed = parseMockArgs(args);
    try {
      if (parsed.error) notify(parsed.error);
      else if (parsed.action === 'list') {
        const result = await api.command({type: 'MockList'});
        showText('Mock scenarios', formatScenarios(result.scenarios || []));
      } else if (parsed.action === 'clean') {
        await api.command({type: 'MockClean'});
        notify('Sandbox restored to its seeded state');
      } else {
        const result = await api.command({type: 'MockStart', scenario: parsed.scenario, speed: parsed.speed, seed: parsed.seed});
        await refreshSessions();
        await openSession(result.session);
      }
    } catch (error) { notify(`Mock failed: ${error.message}`); }
  };
  commands.push(['/mock', 'Run a scripted mock scenario', run, '[list|NAME|clean] [--speed N] [--seed N]']);
  document.body.dataset.dev = 'true';
  const actions = document.querySelector('.topbar-actions');
  if (actions && !document.getElementById('dev-badge')) {
    const badge = document.createElement('span');
    badge.id = 'dev-badge'; badge.className = 'topbar-text dev-badge'; badge.textContent = 'DEV';
    badge.title = 'Dev mode: sandbox workspace, isolated home, /mock scenarios';
    actions.prepend(badge);
  }
  return true;
}
