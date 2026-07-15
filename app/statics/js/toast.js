/* Toast notification system */

function _toastContainer() {
  let c = document.getElementById('toast-container');
  if (!c) {
    c = document.createElement('div');
    c.id = 'toast-container';
    c.className = 'toast-container';
    document.body.appendChild(c);
  }
  return c;
}

function _esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function showToast(message, type = 'success') {
  const tone = type === 'error' ? 'error' : (type === 'info' ? 'info' : 'success');
  const icon = tone === 'success'
    ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>'
    : tone === 'error'
      ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>'
      : '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><circle cx="12" cy="12" r="9"/><path d="M12 8v4"/><path d="M12 16h.01"/></svg>';
  const el = document.createElement('div');
  el.className = `toast toast-${tone}`;
  el.innerHTML = `<div class="toast-icon">${icon}</div><div class="toast-content">${_esc(message)}</div>`;
  _toastContainer().appendChild(el);
  setTimeout(() => {
    el.classList.add('out');
    el.addEventListener('animationend', () => el.remove(), { once: true });
  }, 3000);
}

/**
 * Show a persistent progress toast that stays open until finish() is called.
 *
 * @param {string} label  Initial label text shown inside the toast.
 * @returns {{ update(done, total): void, finish(msg, type?): void, dismiss(): void }}
 *
 * Usage:
 *   const p = showProgressToast('正在刷新 Usage…');
 *   p.update(10, 50);          // update progress bar and counter
 *   p.finish('完成：50 个', 'success');  // switch to result state, auto-dismiss
 */
function showProgressToast(label) {
  const SPIN = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" style="animation:spin .8s linear infinite"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>';

  const el = document.createElement('div');
  el.className = 'toast toast-info';
  el.innerHTML = `
    <div class="toast-icon">${SPIN}</div>
    <div class="toast-content">
      <div class="toast-progress-label">${_esc(label)}</div>
      <div class="toast-progress-track"><div class="toast-progress-fill" style="width:0%"></div></div>
    </div>
    <span class="toast-progress-count"></span>`;
  _toastContainer().appendChild(el);

  const iconEl  = el.querySelector('.toast-icon');
  const labelEl = el.querySelector('.toast-progress-label');
  const fill    = el.querySelector('.toast-progress-fill');
  const countEl = el.querySelector('.toast-progress-count');

  function _dismiss() {
    el.classList.add('out');
    el.addEventListener('animationend', () => el.remove(), { once: true });
  }

  return {
    update(done, total) {
      const pct = total > 0 ? Math.min(100, Math.round(done / total * 100)) : 0;
      fill.style.width = pct + '%';
      countEl.textContent = total > 0 ? `${done} / ${total}` : `${done}`;
    },
    finish(msg, type = 'success') {
      const tone = type === 'error' ? 'error' : 'success';
      const icon = tone === 'success'
        ? '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>'
        : '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
      el.className = `toast toast-${tone}`;
      iconEl.innerHTML = icon;
      fill.style.width = '100%';
      labelEl.textContent = msg;
      countEl.textContent = '';
      setTimeout(_dismiss, 3000);
    },
    dismiss: _dismiss,
  };
}
