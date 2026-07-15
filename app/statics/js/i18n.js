/* Grok2API — i18n */
(function(){
  var KEY='grok2api_lang',
      LANGS={
        zh:{label:'简体中文', html:'zh-CN'},
        en:{label:'English', html:'en'},
        ja:{label:'日本語', html:'ja'},
        es:{label:'Español', html:'es'},
        de:{label:'Deutsch', html:'de'},
        fr:{label:'Français', html:'fr'}
      },
      LANG_CODES=Object.keys(LANGS),
      lang='zh',
      data={},
      ready=false,
      queue=[];

  function normalize(l){
    if(!l) return '';
    l=String(l).toLowerCase();
    if(LANGS[l]) return l;
    l=l.split('-')[0];
    return LANGS[l]?l:'';
  }

  function detect(){
    try{ var s=normalize(localStorage.getItem(KEY)); if(s) return s; }catch(e){}
    var p; try{p=new URLSearchParams(location.search).get('lang')}catch(e){}
    p=normalize(p);
    if(p){try{localStorage.setItem(KEY,p)}catch(e){} return p;}
    var b=normalize(navigator.language||'');
    return b||'zh';
  }

  function get(o,k){
    if(typeof k!=='string' || !k) return;
    for(var p=k.split('.'),i=0;i<p.length;i++){if(o==null)return; o=o[p[i]];}
    return o;
  }

  function merge(base, extra){
    if(extra==null) return base;
    if(base==null || typeof base!=='object' || Array.isArray(base)) return extra;
    var out=Array.isArray(base)?base.slice():Object.assign({}, base), key;
    for(key in extra){
      if(!Object.prototype.hasOwnProperty.call(extra,key)) continue;
      if(out[key] && typeof out[key]==='object' && !Array.isArray(out[key]) && typeof extra[key]==='object' && !Array.isArray(extra[key])){
        out[key]=merge(out[key], extra[key]);
      }else{
        out[key]=extra[key];
      }
    }
    return out;
  }

  function fetchLang(l){
    return fetch('/static/i18n/'+l+'.json', {cache:'no-store'}).then(function(r){return r.ok?r.json():{};}).catch(function(){return {};});
  }

  function t(k,p){
    var v=get(data,k); if(v===undefined) return k;
    if(p) Object.keys(p).forEach(function(n){v=v.replace(new RegExp('\\{'+n+'\\}','g'),p[n]);});
    return v;
  }

  function apply(root){
    var c=root||document;
    c.querySelectorAll('[data-i18n]').forEach(function(el){ var v=get(data,el.getAttribute('data-i18n')); if(v!==undefined) el.textContent=v; });
    c.querySelectorAll('[data-i18n-placeholder]').forEach(function(el){ var v=get(data,el.getAttribute('data-i18n-placeholder')); if(v!==undefined) el.placeholder=v; });
    c.querySelectorAll('[data-i18n-title]').forEach(function(el){ var v=get(data,el.getAttribute('data-i18n-title')); if(v!==undefined) el.title=v; });
    c.querySelectorAll('[data-i18n-aria-label]').forEach(function(el){ var v=get(data,el.getAttribute('data-i18n-aria-label')); if(v!==undefined) el.setAttribute('aria-label', v); });
  }

  function init(){
    lang=detect();
    document.documentElement.lang=(LANGS[lang]&&LANGS[lang].html)||lang;
    Promise.all([fetchLang('en'), lang==='en'?Promise.resolve({}):fetchLang(lang)]).then(function(parts){
      data=lang==='en'?parts[0]:merge(parts[0], parts[1]);
      ready=true; apply(document); queue.forEach(function(cb){cb()}); queue=[];
    });
  }

  function setLang(l){ l=normalize(l); if(!l)return; try{localStorage.setItem(KEY,l)}catch(e){} location.reload(); }
  function toggleLang(){
    var idx=LANG_CODES.indexOf(lang);
    setLang(LANG_CODES[(idx+1)%LANG_CODES.length]);
  }

  window.I18n={
    t:t,
    apply:apply,
    setLang:setLang,
    toggleLang:toggleLang,
    getLang:function(){return lang},
    getLanguages:function(){ return LANG_CODES.map(function(code){ return { code:code, label:LANGS[code].label }; }); },
    onReady:function(cb){if(ready)cb();else queue.push(cb)}
  };
  window.t=t;
  document.readyState==='loading'?document.addEventListener('DOMContentLoaded',init):init();
})();
