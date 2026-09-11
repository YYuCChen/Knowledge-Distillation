/* A single visible dialog owns focus and its result. Content is always text. */
window.kdDialog = (message, {confirm = false} = {}) => new Promise(resolve => {
  if (document.querySelector('.app-dialog[open]')) { resolve(false); return; }
  const previous = document.activeElement;
  const dialog = document.createElement('dialog');
  dialog.className = 'app-dialog';
  dialog.setAttribute('aria-labelledby', 'app-dialog-title');
  dialog.setAttribute('aria-describedby', 'app-dialog-message');
  const title = document.createElement('h2');
  title.id = 'app-dialog-title';
  title.textContent = confirm ? '重新识别素材' : '操作提示';
  const body = document.createElement('p');
  body.id = 'app-dialog-message';
  body.textContent = message;
  const actions = document.createElement('div');
  actions.className = 'app-dialog-actions';
  if (confirm) {
    const cancel = document.createElement('button');
    cancel.type = 'button'; cancel.className = 'secondary-button';
    cancel.textContent = '取消'; cancel.autofocus = true;
    cancel.addEventListener('click', () => finish(false));
    actions.append(cancel);
  }
  const accept = document.createElement('button');
  accept.type = 'button'; accept.className = 'primary-button';
  accept.textContent = confirm ? '重新识别' : '知道了';
  accept.autofocus = !confirm;
  accept.addEventListener('click', () => finish(true));
  actions.append(accept);
  dialog.append(title, body, actions);
  let settled = false;
  function finish(accepted) {
    if (settled) return;
    settled = true;
    if (dialog.open) dialog.close();
    dialog.remove();
    if (previous?.isConnected) previous.focus({preventScroll: true});
    resolve(accepted);
  }
  // Finish directly: background tabs can defer the native close event.
  dialog.addEventListener('cancel', event => { event.preventDefault(); finish(false); });
  dialog.addEventListener('close', () => finish(dialog.returnValue === 'accept'), {once: true});
  document.body.append(dialog);
  dialog.showModal();
});
