document.addEventListener('change', event => {
  if (event.target.name !== 'mode') return;
  document.querySelectorAll('input[name="collection"]').forEach(input => {
    input.disabled = event.target.value !== 'selected' || input.dataset.empty === 'true';
  });
});
document.addEventListener('submit', event => {
  if (!event.target.matches('[data-scope-form]')) return;
  const button = event.target.querySelector('button[type="submit"]');
  if (button) { button.disabled = true; button.textContent = '正在核对范围'; }
});
