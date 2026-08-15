(() => {
  'use strict';

  const form = document.getElementById('loginForm');
  const username = document.getElementById('username');
  const password = document.getElementById('password');
  const button = document.getElementById('loginButton');
  const error = document.getElementById('loginError');

  function showError(message) {
    error.textContent = message || 'Sign in failed';
    error.hidden = false;
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    error.hidden = true;
    button.disabled = true;

    try {
      const response = await fetch('/auth/login', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-UDM-Web': '1',
        },
        credentials: 'same-origin',
        cache: 'no-store',
        body: JSON.stringify({
          username: username.value.trim(),
          password: password.value,
        }),
      });

      let data = {};
      try {
        data = await response.json();
      } catch {
        data = { ok: false, error: `Unexpected response (${response.status})` };
      }

      if (!response.ok || data.ok === false) {
        showError(data.error || `Sign in failed (${response.status})`);
        password.value = '';
        password.focus();
        return;
      }

      password.value = '';
      location.replace(data.redirect || '/app');
    } catch (err) {
      showError(err && err.message ? err.message : 'Could not reach UDM');
    } finally {
      button.disabled = false;
    }
  });
})();
