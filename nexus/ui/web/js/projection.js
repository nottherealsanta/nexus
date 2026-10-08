function decodePointer(path){
  if(typeof path!=='string'||(path!==''&&!path.startsWith('/')))throw new Error('Invalid patch path');
  if(/~(?:[^01]|$)/.test(path))throw new Error('Invalid patch pointer escape');
  return path===''?[]:path.slice(1).split('/').map(part=>part.replaceAll('~1','/').replaceAll('~0','~'));
}
function clone(value){
  if(Array.isArray(value))return value.map(clone);
  if(value&&typeof value==='object')return Object.fromEntries(Object.entries(value).map(([k,v])=>[k,clone(v)]));
  return value;
}
// Copy-on-write: only containers on a patched path are copied (once per batch), so
// untouched subtrees keep their identity and renderers can skip them cheaply.
function own(node,copied){
  if(copied.has(node))return node;
  const copy=Array.isArray(node)?node.slice():{...node};copied.add(copy);return copy;
}
function stage(root,item,copied){
  if(!item||typeof item!=='object'||!['add','replace','remove','append'].includes(item.op))throw new Error('Unsupported patch operation');
  const parts=decodePointer(item.path);if(!parts.length){if(item.op==='remove')return {};if(item.op==='append')throw new Error('Invalid root append');return clone(item.value);}
  if(root===null||typeof root!=='object')throw new Error('Invalid patch path');
  root=own(root,copied);let target=root;
  for(const part of parts.slice(0,-1)){if(['__proto__','prototype','constructor'].includes(part)||target===null||typeof target!=='object'||!Object.hasOwn(target,part))throw new Error('Invalid patch path');const child=target[part];if(child===null||typeof child!=='object')throw new Error('Invalid patch path');target=target[part]=own(child,copied);}
  const key=parts.at(-1);
  if(['__proto__','prototype','constructor'].includes(key)||target===null||typeof target!=='object')throw new Error('Invalid patch target');
  if(Array.isArray(target)){
    const index=key==='-'?target.length:/^(0|[1-9]\d*)$/.test(key)?Number(key):-1;
    if(item.op==='add'){if(index<0||index>target.length)throw new Error('Invalid array patch');target.splice(index,0,clone(item.value));}
    else {if(index<0||index>=target.length)throw new Error('Invalid array patch');if(item.op==='remove')target.splice(index,1);else if(item.op==='replace')target[index]=clone(item.value);else throw new Error('Invalid append target');}
  }else{
    if(item.op==='append'){if(typeof target[key]!=='string'||typeof item.value!=='string')throw new Error('Invalid append patch');target[key]+=item.value;}
    else if(item.op==='remove'){if(!Object.hasOwn(target,key))throw new Error('Invalid patch path');delete target[key];}
    else {if(item.op==='replace'&&!Object.hasOwn(target,key))throw new Error('Invalid patch path');target[key]=clone(item.value);}
  }
  return root;
}
// Validate and apply without mutating `root`. Callers can commit the returned tree
// only after every operation succeeds; the last-good projection is untouched.
// Unchanged subtrees are shared with `root` (treat both as immutable).
export function applyOperations(root,operations){
  if(!Array.isArray(operations))throw new Error('Invalid patch operations');
  const copied=new WeakSet();let staged=root;for(const item of operations)staged=stage(staged,item,copied);return staged;
}
