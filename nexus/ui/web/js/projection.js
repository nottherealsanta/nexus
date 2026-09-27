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
function stage(root,item){
  if(!item||typeof item!=='object'||!['add','replace','remove','append'].includes(item.op))throw new Error('Unsupported patch operation');
  const parts=decodePointer(item.path);if(!parts.length){if(item.op==='remove')return {};if(item.op==='append')throw new Error('Invalid root append');return clone(item.value);}
  let target=root;
  for(const part of parts.slice(0,-1)){if(['__proto__','prototype','constructor'].includes(part)||target===null||typeof target!=='object'||!(part in target))throw new Error('Invalid patch path');target=target[part];}
  const key=parts.at(-1);
  if(['__proto__','prototype','constructor'].includes(key)||target===null||typeof target!=='object')throw new Error('Invalid patch target');
  if(Array.isArray(target)){
    const index=key==='-'?target.length:/^(0|[1-9]\d*)$/.test(key)?Number(key):-1;
    if(item.op==='add'){if(index<0||index>target.length)throw new Error('Invalid array patch');target.splice(index,0,clone(item.value));}
    else {if(index<0||index>=target.length)throw new Error('Invalid array patch');if(item.op==='remove')target.splice(index,1);else if(item.op==='replace')target[index]=clone(item.value);else throw new Error('Invalid append target');}
  }else{
    if(item.op==='append'){if(typeof target[key]!=='string'||typeof item.value!=='string')throw new Error('Invalid append patch');target[key]+=item.value;}
    else if(item.op==='remove'){if(!(key in target))throw new Error('Invalid patch path');delete target[key];}
    else {if(item.op==='replace'&&!(key in target))throw new Error('Invalid patch path');target[key]=clone(item.value);}
  }
  return root;
}
// Validate and apply on a detached copy. Callers can commit the returned tree
// only after every operation succeeds; the last-good projection is untouched.
export function applyOperations(root,operations){
  if(!Array.isArray(operations))throw new Error('Invalid patch operations');
  let staged=clone(root);for(const item of operations)staged=stage(staged,item);return staged;
}
