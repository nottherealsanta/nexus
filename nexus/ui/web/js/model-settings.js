// One-page global model settings, using the same host commands as the native shell.
export function createModelSettings({api, el, root, notify, pickModel, isOpen}) {
  let generation = 0, data = null, tier = '', busy = false;
  const button = (text, run, disabled = false) => {
    const node = el('button', 'toolbar-button', text);
    node.type = 'button'; node.disabled = busy || disabled; node.onclick = run;
    return node;
  };
  const note = text => el('p', 'settings-help', text);
  async function load() {
    const request = ++generation;
    root.setAttribute('aria-busy', 'true');
    if (!data) root.replaceChildren(note('Loading model settings…'));
    try {
      const [defaults, titles, tiers] = await Promise.all([
        api.command({type: 'DefaultModelSettings'}),
        api.command({type: 'SessionTitleSettings'}),
        api.command({type: 'ModelTiers'}),
      ]);
      if (request !== generation || !isOpen()) return;
      data = {defaults, titles, tiers};
      if (!tiers.order.includes(tier)) tier = tiers.order[0] || '';
      render();
    } catch (error) {
      if (request !== generation || !isOpen()) return;
      root.replaceChildren(note(`Could not load model settings: ${error.message}`), button('Retry', load));
    } finally { if (request === generation) root.removeAttribute('aria-busy'); }
  }
  async function save(command) {
    if (busy || !isOpen()) return;
    busy = true; render();
    try {
      const result = await api.command(command);
      if (result.restart_required) notify('Saved. Restart the daemon when active turns finish to apply these settings.');
      await load();
    } catch (error) { notify(`Could not save model settings: ${error.message}`); }
    finally { busy = false; if (isOpen() && data) render(); }
  }
  function ordered(title, refs, resolved, command, {editable = true, candidates = []} = {}) {
    const section = el('section', 'model-settings-section');
    section.append(el('h4', '', title));
    const list = el('ol', 'model-chain');
    refs.forEach((ref, index) => {
      const row = el('li', 'model-chain-row'), text = el('div', 'model-chain-text');
      const candidate = candidates.find(value => value.ref === ref);
      text.append(el('strong', '', ref), note(ref === resolved ? 'In use' : candidate?.connected === false ? `Skipped · ${candidate.reason || 'provider not connected'}` : 'Fallback'));
      const change = next => save({...command, refs: next});
      const move = delta => {const next = [...refs]; [next[index], next[index + delta]] = [next[index + delta], next[index]]; change(next);};
      row.append(text, button('↑', () => move(-1), !editable || index === 0), button('↓', () => move(1), !editable || index === refs.length - 1), button('Remove', () => change(refs.filter((_, i) => i !== index)), !editable || refs.length <= 1));
      row.querySelectorAll('button').forEach((node, i) => node.setAttribute('aria-label', `${['Move up', 'Move down', 'Remove'][i]} ${ref}`));
      row.tabIndex = 0;
      row.onkeydown = event => {
        if (event.target !== row || busy || !editable) return;
        if (event.altKey && ['ArrowUp', 'ArrowDown'].includes(event.key)) {
          event.preventDefault(); const delta = event.key === 'ArrowUp' ? -1 : 1;
          if (index + delta >= 0 && index + delta < refs.length) move(delta);
        } else if (event.key === 'Delete' && refs.length > 1) {event.preventDefault(); change(refs.filter((_, i) => i !== index));}
      };
      list.append(row);
    });
    section.append(list, button('Add model…', () => {
      const request = generation;
      pickModel(ref => {if (request === generation && isOpen() && !refs.includes(ref)) save({...command, refs: [...refs, ref]});});
    }, !editable));
    return section;
  }
  function selectRow(label, options, value, change) {
    const row = el('label', 'settings-row'), select = el('select');
    select.setAttribute('aria-label', label); select.disabled = busy;
    for (const [text, ref] of options) {const option = el('option', '', text); option.value = ref; select.append(option);}
    select.value = value; select.onchange = () => change(select.value);
    row.append(el('span', 'settings-label', label), select); return row;
  }
  function render() {
    if (!data) return;
    const focus = document.activeElement, focusLabel = root.contains(focus) ? focus.getAttribute('aria-label') : null;
    const {defaults, titles, tiers} = data;
    root.replaceChildren(el('h3', '', 'Models'), note('Global settings. Ordered lists use the first model whose provider can run.'));
    root.append(ordered('Default model', defaults.refs || [], defaults.resolved, {type: 'DefaultModelSet'}, {candidates: defaults.candidates || []}), note(defaults.message || `New sessions run on ${defaults.resolved || 'no runnable model'}.`));
    const titleSection = el('section', 'model-settings-section'), toggleRow = el('label', 'settings-row'), toggle = el('input');
    toggle.type = 'checkbox'; toggle.checked = titles.enabled; toggle.disabled = busy; toggle.setAttribute('role', 'switch'); toggle.setAttribute('aria-label', 'Name sessions automatically');
    toggle.onchange = () => save({type: 'SessionTitleSettingsSet', enabled: toggle.checked});
    toggleRow.append(el('span', 'settings-label', 'Name sessions automatically'), toggle);
    const titleOptions = tiers.order.map(name => [`${name} tier`, name]);
    if (!tiers.order.includes(titles.model)) titleOptions.push([titles.model, titles.model]);
    titleOptions.push(['Other model…', '__pick__']);
    titleSection.append(el('h4', '', 'Session titles'), toggleRow, selectRow('Title model', titleOptions, titles.model, model => {
      if (model !== '__pick__') save({type: 'SessionTitleSettingsSet', model});
      else {render(); const request = generation; pickModel(ref => {if (request === generation && isOpen()) save({type: 'SessionTitleSettingsSet', model: ref});});}
    }), note(titles.resolved ? `Runs on ${titles.resolved}. Off keeps the first line of the first message.` : titles.message || 'No title model can run.'));
    root.append(titleSection);
    const tabs = el('div', 'model-tier-tabs'); tabs.setAttribute('role', 'tablist'); tabs.setAttribute('aria-label', 'Model tiers');
    tiers.order.forEach(name => {const tab = button(name, () => {tier = name; render();}); tab.id = `model-tier-${name}`; tab.setAttribute('role', 'tab'); tab.setAttribute('aria-selected', String(name === tier)); tab.tabIndex = name === tier ? 0 : -1; tab.onkeydown = event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault(); const index = tiers.order.indexOf(name);
      tier = event.key === 'Home' ? tiers.order[0] : event.key === 'End' ? tiers.order.at(-1) : tiers.order[(index + (event.key === 'ArrowRight' ? 1 : -1) + tiers.order.length) % tiers.order.length]; render(); root.querySelector(`#model-tier-${CSS.escape(tier)}`)?.focus();
    }; tabs.append(tab);});
    root.append(el('h4', '', 'Tiers'), tabs);
    const row = (tiers.tiers || []).find(value => value.name === tier);
    if (row) {
      const section = ordered(`${tier} models`, row.refs || [], row.resolved, {type: 'ModelTierSet', tier}, {editable: row.editable !== false, candidates: row.candidates || []});
      section.setAttribute('role', 'tabpanel'); section.setAttribute('aria-labelledby', `model-tier-${tier}`);
      section.append(note(`${row.source} · ${row.resolved || 'No runnable model'}`), button('Reset tier to default', () => {
        if (confirm(`Reset ${tier} to the built-in model choices?`)) save({type: 'ModelTierReset', tier});
      }, row.editable === false)); root.append(section);
    }
    root.append(selectRow('Highest tier for subagents', tiers.order.map(name => [name, name]), tiers.max_tier, value => save({type: 'AgentMaxTierSet', tier: value})), button('Refresh model catalogue', () => save({type: 'ModelsRefresh'})));
    if (focusLabel) [...root.querySelectorAll('[aria-label]')].find(node => node.getAttribute('aria-label') === focusLabel && !node.disabled)?.focus({preventScroll: true});
  }
  return {load, invalidate: () => {generation++;}};
}
