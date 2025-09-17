const MM = (function(){
  let modal, viewer, carousel, items=[], current=0;
  let cache = new Map();
  let totalCacheBytes = 0;
  let cacheLimitMB = 50; // default cache limit in MB
  let preloadCount = 2; // default number of images to preload in each direction
  let _renderSeq = 0; // sequence counter to avoid race when rendering async
  let basePreload = 2;
  let lastDirection = null; // 'next' or 'prev'
  let streakCount = 0;
  const maxStreak = 5; // cap how many extra preload levels
  let streakTimer = null;
  const streakTimeoutMs = 1500; // reset streak after inactivity
  // spinner elements
  let mmSpinner = null;
  let mmSpinnerText = null;

  function formatBytes(bytes) {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
  }

  async function preloadImage(url) {
    if (cache.has(url)){
      const entry = cache.get(url);
      entry.lastUsed = Date.now();
      return entry.objURL;
    }
    // fetch and measure size if possible
    let res;
    try{
      res = await fetch(url);
    }catch(err){
      const errEntry = { objURL: null, size:0, status:0, lastUsed:Date.now(), error: err };
      cache.set(url, errEntry);
      return Promise.reject({ status: 0, message: err.message || 'fetch error' });
    }
    const status = res.status;
    let blob;
    try{
      blob = await res.blob();
    }catch(err){
      const errEntry = { objURL: null, size:0, status, lastUsed:Date.now(), error: err };
      cache.set(url, errEntry);
      return Promise.reject({ status, message: err.message || 'blob error' });
    }
    const size = blob.size || 0;
    const objURL = URL.createObjectURL(blob);
    const entry = { objURL, size, status, lastUsed: Date.now() };
    cache.set(url, entry);
    totalCacheBytes += size;
    // ensure cache limit
    ensureCacheLimit();
    if(status>=200 && status < 300) return objURL;
    // status error
    return Promise.reject({ status, message: 'HTTP '+status });
  }

  function ensureCacheLimit(requiredUrls=[]){
    // keep totalCacheBytes <= cacheLimitMB
    const limitBytes = Math.max(0, cacheLimitMB) * 1024 * 1024;
    if(totalCacheBytes <= limitBytes) return;
    // build list of candidates excluding requiredUrls
    const requiredSet = new Set(requiredUrls || []);
    // collect entries sorted by lastUsed asc
    const entries = [];
    for(const [url,entry] of cache.entries()){
      if(requiredSet.has(url)) continue; // never evict required
      if(!entry) continue;
      entries.push({url, lastUsed: entry.lastUsed || 0, size: entry.size || 0, entry});
    }
    entries.sort((a,b)=>a.lastUsed - b.lastUsed);
    // evict LRU until under limit
    for(const e of entries){
      if(totalCacheBytes <= limitBytes) break;
      try{ URL.revokeObjectURL(e.entry.objURL); }catch(e){}
      cache.delete(e.url);
      totalCacheBytes -= e.size || 0;
    }
  }

  // preload neighbor images around center index (both directions)
  function preloadNeighbors(centerIndex, count){
    if(!items || items.length===0) return;
    const n = Math.max(0, Math.floor(count)||0);
    const len = items.length;
    const requiredUrls = [];
    for(let i=1;i<=n;i++){
      const nextIdx = (centerIndex + i) % len;
      const prevIdx = (centerIndex - i + len) % len;
      [nextIdx, prevIdx].forEach(idx=>{
        const it = items[idx];
        if(!it) return;
        if(it.type && it.type.startsWith('image')){
          requiredUrls.push(it.url);
          // fire-and-forget preload
          preloadImage(it.url).catch(()=>{});
        }
      });
    }
    // also protect the center image
    const center = items[centerIndex];
    if(center && center.type && center.type.startsWith('image')) requiredUrls.push(center.url);
    // ensure cache limit but keep required urls
    ensureCacheLimit(requiredUrls);
  }

  async function render(index){
    current = index;
    const mySeq = ++_renderSeq;
    const it = items[index];
    // clear viewer and recreate content + nav buttons so handlers remain attached
    // hide any previous spinner text and show spinner while loading
    viewer.innerHTML = '';
    if(mmSpinner) { mmSpinner.classList.remove('hidden'); if(mmSpinnerText) mmSpinnerText.textContent = 'Cargando...'; }
    // title/size
    document.querySelector(".mm-title").textContent = it.title || "(Sin título)";
    document.querySelector(".mm-size").textContent = it.size ? formatBytes(it.size) : "";

    // create nav buttons (they are invisible but interactive)
    const btnPrev = document.createElement('button');
    btnPrev.className = 'mm-nav-btn prev';
    btnPrev.id = 'mmPrev';
    btnPrev.setAttribute('aria-label','Anterior');
    const btnNext = document.createElement('button');
    btnNext.className = 'mm-nav-btn next';
    btnNext.id = 'mmNext';
    btnNext.setAttribute('aria-label','Siguiente');
    // attach nav handlers
    btnPrev.addEventListener('click', prev);
    btnNext.addEventListener('click', next);

    // media element
    if(it.type.startsWith("image")){
      // ensure the current image is preloaded first
      try{
        const src = await preloadImage(it.url);
        // abort if a newer render started
        if(mySeq !== _renderSeq) return;
        const img=document.createElement("img");
        img.src=src;
        img.alt=it.title||"";
        // append elements in order: prev, image, next
        viewer.appendChild(btnPrev);
        viewer.appendChild(img);
        viewer.appendChild(btnNext);
        // hide spinner now that image is shown
        if(mmSpinner) mmSpinner.classList.add('hidden');
        // start preloading neighbors in background - dynamic based on user's streak
        const dynamicCount = Math.min(10, Math.max(0, basePreload + Math.max(0, streakCount-1)));
        setTimeout(()=>preloadNeighbors(index, dynamicCount),0);
      }catch(e){
        // show error in spinner area and keep nav buttons so user can navigate
        if(mmSpinnerText) mmSpinnerText.textContent = (e && e.status) ? ('Error HTTP ' + e.status) : (e && e.message) ? e.message : 'Error al cargar';
        if(mmSpinner) mmSpinner.classList.remove('hidden');
        viewer.appendChild(btnPrev);
        const errBox = document.createElement('div');
        errBox.style.color = '#f66';
        errBox.style.padding = '8px 12px';
        errBox.style.borderRadius = '8px';
        errBox.textContent = mmSpinnerText ? mmSpinnerText.textContent : 'Error al cargar';
        viewer.appendChild(errBox);
        viewer.appendChild(btnNext);
      }
    } else if(it.type.startsWith("video")){
      const vid=document.createElement("video");
      vid.src=it.url;
      vid.controls=true;
      vid.autoplay=true;
      // append nav buttons but keep them visually invisible; videos remain as-is
      viewer.appendChild(btnPrev);
      viewer.appendChild(vid);
      viewer.appendChild(btnNext);
      // hide any spinner when video starts
      if(mmSpinner) mmSpinner.classList.add('hidden');
    }

    [...carousel.children].forEach((c,i)=>c.classList.toggle("active", i===index));
  }

  function prev(){
    if(items.length===0) return;
    const nextIndex = (current - 1 + items.length) % items.length;
    // update streak
    updateStreak('prev');
    render(nextIndex);
  }

  function next(){
    if(items.length===0) return;
    const nextIndex = (current + 1) % items.length;
    // update streak
    updateStreak('next');
    render(nextIndex);
  }

  function updateStreak(dir){
    if(lastDirection === dir){
      streakCount = Math.min(maxStreak, streakCount + 1);
    } else {
      streakCount = 1;
      lastDirection = dir;
    }
    // reset timer
    if(streakTimer) clearTimeout(streakTimer);
    streakTimer = setTimeout(()=>{ lastDirection=null; streakCount=0; }, streakTimeoutMs);
  }

  function openModal(data, startIndex=0){
    items = data;
    current = startIndex;
    carousel.innerHTML="";
    items.forEach((it,i)=>{
      const thumb=document.createElement("div");
      thumb.className="mm-thumb";
      const img=document.createElement("img");
      img.src=it.thumbnail||it.url;
      thumb.appendChild(img);
      thumb.onclick=()=>render(i);
      carousel.appendChild(thumb);
    });
    modal.classList.add("active");
    render(startIndex);
    // focus for keyboard navigation
    setTimeout(()=> modal.focus?.(),50);
  }

  // openModal with options: {startIndex, carousel: 'bottom'|'top'|'left'|'right'}
  function openModalWithOptions(data, opts={}){
    const startIndex = opts.startIndex||0;
    const pos = opts.carousel||'bottom';
    if(typeof opts.preload === 'number') preloadCount = Math.max(0, Math.floor(opts.preload));
    // reset classes
    modal.classList.remove('locked');
    modal.classList.remove('with-side-carousel');
    carousel.classList.remove('left','right');

    if(pos==='left' || pos==='right'){
      carousel.classList.add(pos);
      modal.classList.add('with-side-carousel');
    }
    // for top/bottom keep default absolute bottom
    openModal(data, startIndex);
  }

  function closeModal(){
    modal.classList.remove("active");
    viewer.innerHTML="";
  }

  function init(){
    modal=document.getElementById("mmModal");
    viewer=document.getElementById("mmViewer");
    carousel=document.getElementById("mmCarousel");
    mmSpinner = document.getElementById('mmSpinner');
    mmSpinnerText = mmSpinner ? mmSpinner.querySelector('.mm-spinner-text') : null;
    document.getElementById("btnClose").onclick=closeModal;
    // wire invisible nav buttons
    const btnPrev = document.getElementById('mmPrev');
    const btnNext = document.getElementById('mmNext');
    if(btnPrev) btnPrev.addEventListener('click', prev);
    if(btnNext) btnNext.addEventListener('click', next);
    document.getElementById("btnFullscreen").onclick=()=>{
      if(!document.fullscreenElement){
        modal.requestFullscreen();
      } else {
        document.exitFullscreen();
      }
    };
    const btnLock = document.getElementById('btnLock');
    const buttonsContainer = btnLock.parentElement; // original container (.mm-buttons)
    document.getElementById("btnLock").onclick=()=>{
      // toggle locked state on modal: hide UI except lock button and keep nav buttons active
      const isLocked = modal.classList.toggle('locked');
      if(isLocked){
        // make viewer slightly transparent to indicate lock
        viewer.style.opacity = '1';
        // move lock button to document body so it's above nav buttons and images
        try{
          document.body.appendChild(btnLock);
          // ensure it's fixed and on top
          btnLock._savedStyle = {
            position: btnLock.style.position || '',
            right: btnLock.style.right || '',
            top: btnLock.style.top || '',
            zIndex: btnLock.style.zIndex || '',
            opacity: btnLock.style.opacity || ''
          };
          btnLock.style.position = 'fixed';
          btnLock.style.right = '16px';
          btnLock.style.top = '12px';
          btnLock.style.zIndex = '100000';
          btnLock.style.opacity = '0.15';
          btnLock.style.background = "black";
        }catch(e){}
      } else {
        viewer.style.opacity = '';
        // restore lock button to original container and styles
        try{
          buttonsContainer.appendChild(btnLock);
          if(btnLock._savedStyle){
            btnLock.style.position = btnLock._savedStyle.position;
            btnLock.style.right = btnLock._savedStyle.right;
            btnLock.style.top = btnLock._savedStyle.top;
            btnLock.style.zIndex = btnLock._savedStyle.zIndex;
            btnLock.style.opacity = btnLock._savedStyle.opacity;
            delete btnLock._savedStyle;
          }
        }catch(e){}
      }
    };

    // keyboard navigation while modal open
    document.addEventListener('keydown', (e)=>{
      if(!modal.classList.contains('active')) return;
      if(e.key === 'ArrowLeft') { e.preventDefault(); prev(); }
      else if(e.key === 'ArrowRight') { e.preventDefault(); next(); }
      else if(e.key === 'Escape') { e.preventDefault(); closeModal(); }
    });
  }

  return { init, openModal, openModalWithOptions };
})();

window.addEventListener("DOMContentLoaded", ()=>{
  MM.init();
  //document.getElementById("btnDemo").onclick=()=>{
    //MM.openModalWithOptions([
      //{title:"Imagen 1", url:"https://picsum.photos/800/600?1", type:"image/jpeg", size: 53212},
      //{title:"Imagen 1", url:"/car/archivo6311.jpg", type:"image/jpeg", size: 53212},
      //{title:"Imagen 1", url:"/car/archivo7111.jpg", type:"image/jpeg", size: 53212},
      //{title:"Imagen 1", url:"/car/archivo8111.jpg", type:"image/jpeg", size: 53212},
      //{title:"Imagen 1", url:"/car/archivo9111.jpg", type:"image/jpeg", size: 53212},
      //{title:"Imagen 2", url:"https://picsum.photos/800/600?2", type:"image/jpeg", size: 102312},
      //{title:"Video demo", url:"https://www.w3schools.com/html/mov_bbb.mp4", type:"video/mp4", size: 2321123},
    //], { startIndex: 3, carousel: 'bottom', preload: 2 });
  //};
});