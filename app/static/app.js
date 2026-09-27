document.querySelectorAll('form').forEach(form => {
  form.addEventListener('submit', event => {
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      event.preventDefault(); return;
    }
    const button = event.submitter;
    if (button) {
      // Disable only after successful browser validation; server transactions remain authoritative.
      setTimeout(() => { button.disabled = true; if (button.dataset.loading) button.textContent = button.dataset.loading; }, 0);
    }
  });
});
document.querySelectorAll('[data-back]').forEach(button => button.addEventListener('click', () => history.back()));
window.addEventListener('pageshow', () => document.querySelectorAll('form button').forEach(button => { button.disabled = false; }));
