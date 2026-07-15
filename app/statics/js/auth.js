/* Grok2API — Auth module */
const ADMIN_API = '/admin/api';
const WEBUI_API = '/webui/api';

const _ENC = new TextEncoder(), _DEC = new TextDecoder();
const _SECRET = 'grok2api-admin-key';
const _XOR_P = 'enc:xor:', _AES_P = 'enc:v1:';

function _toB64(b) { let s=''; b.forEach(v=>s+=String.fromCharCode(v)); return btoa(s); }
function _fromB64(s) { const d=atob(s), a=new Uint8Array(d.length); for(let i=0;i<d.length;i++) a[i]=d.charCodeAt(i); return a; }
function _xor(d,k) { const o=new Uint8Array(d.length); for(let i=0;i<d.length;i++) o[i]=d[i]^k[i%k.length]; return o; }

async function _deriveKey(salt) {
  const km = await crypto.subtle.importKey('raw',_ENC.encode(_SECRET),'PBKDF2',false,['deriveKey']);
  return crypto.subtle.deriveKey({name:'PBKDF2',salt,iterations:100000,hash:'SHA-256'},km,{name:'AES-GCM',length:256},false,['encrypt','decrypt']);
}

async function _encrypt(plain) {
  if (!plain) return '';
  if (!crypto?.subtle) return _XOR_P+_toB64(_xor(_ENC.encode(plain),_ENC.encode(_SECRET)));
  const salt=crypto.getRandomValues(new Uint8Array(16)), iv=crypto.getRandomValues(new Uint8Array(12));
  const key=await _deriveKey(salt), ct=await crypto.subtle.encrypt({name:'AES-GCM',iv},key,_ENC.encode(plain));
  return `${_AES_P}${_toB64(salt)}:${_toB64(iv)}:${_toB64(new Uint8Array(ct))}`;
}

async function _decrypt(s) {
  if (!s) return '';
  if (s.startsWith(_XOR_P)) return _DEC.decode(_xor(_fromB64(s.slice(_XOR_P.length)),_ENC.encode(_SECRET)));
  if (!s.startsWith(_AES_P)||!crypto?.subtle) return '';
  const p=s.split(':'); if(p.length!==5) return '';
  const key=await _deriveKey(_fromB64(p[2]));
  return _DEC.decode(await crypto.subtle.decrypt({name:'AES-GCM',iv:_fromB64(p[3])},key,_fromB64(p[4])));
}

/* Key store factory */
function _keyStore(k) {
  return {
    get:   async()=>{ const s=localStorage.getItem(k)||''; if(!s)return''; try{return await _decrypt(s)}catch{localStorage.removeItem(k);return''} },
    set:   async(v)=>{ if(!v){localStorage.removeItem(k);return} localStorage.setItem(k,await _encrypt(v)||'') },
    clear: ()=>localStorage.removeItem(k),
  };
}

const adminKey = _keyStore('grok2api_admin_key');
const webuiKey = _keyStore('grok2api_webui_key');

async function verifyKey(url, key) {
  return (await fetch(url, { headers: key ? { Authorization: `Bearer ${key}` } : {} })).ok;
}

function adminLogout() { adminKey.clear(); webuiKey.clear(); location.href='/admin/login'; }
function webuiLogout() { webuiKey.clear(); location.href='/webui/login'; }
