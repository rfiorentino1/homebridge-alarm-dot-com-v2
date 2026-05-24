/* eslint-disable no-undef */
// Frontend state machine for the AlarmDotComV2 setup UI. Communicates with
// homebridge-ui/server.js via the homebridge.request() bridge. Persists the
// final credentials + mfaCookie back into the plugin config when done.

(() => {
  const STATE = {
    username: '',
    password: '',
    method: '',
    hints: {},
    availableMethods: [],
  };

  const PLATFORM_NAME = 'AlarmDotComV2';

  function $(sel) {
    return document.querySelector(sel);
  }
  function $$(sel) {
    return document.querySelectorAll(sel);
  }

  function showStep(name) {
    $$('section[data-step]').forEach((s) => {
      s.hidden = s.dataset.step !== name;
    });
  }

  function setError(msg) {
    $('#error-detail').textContent = msg;
    showStep('error');
  }

  async function loadExistingConfig() {
    try {
      const blocks = await homebridge.getPluginConfig();
      const current = (blocks || []).find((b) => b && b.platform === PLATFORM_NAME);
      if (current) {
        if (current.username) {
          $('#f-username').value = current.username;
          STATE.username = current.username;
        }
        if (current.password) {
          $('#f-password').value = current.password;
          STATE.password = current.password;
        }
      }
    } catch (e) {
      // Non-fatal — first-time setup has no existing config.
    }
  }

  async function saveConfig({ username, password, mfaCookie }) {
    const blocks = (await homebridge.getPluginConfig()) || [];
    const idx = blocks.findIndex((b) => b && b.platform === PLATFORM_NAME);
    const updated = { ...(idx >= 0 ? blocks[idx] : {}), platform: PLATFORM_NAME, username, password };
    if (mfaCookie) updated.mfaCookie = mfaCookie;
    if (idx >= 0) blocks[idx] = updated;
    else blocks.push(updated);
    await homebridge.updatePluginConfig(blocks);
    await homebridge.savePluginConfig();
  }

  function methodLabel(name) {
    if (name === 'sms') return 'Text message (SMS)';
    if (name === 'email') return 'Email';
    if (name === 'app') return 'Authenticator app';
    return name;
  }

  function methodHintSuffix(name) {
    const h = STATE.hints[name];
    if (!h) return '';
    return ` — ${h}`;
  }

  function renderOtpMethods() {
    const wrap = $('#otp-methods');
    wrap.innerHTML = '';
    STATE.availableMethods.forEach((m, i) => {
      const id = `m-${m}`;
      const row = document.createElement('div');
      row.className = 'form-check';
      row.innerHTML = `
        <input class="form-check-input" type="radio" name="otp-method" id="${id}" value="${m}" ${i === 0 ? 'checked' : ''}>
        <label class="form-check-label" for="${id}">${methodLabel(m)}${methodHintSuffix(m)}</label>
      `;
      wrap.appendChild(row);
    });
    STATE.method = STATE.availableMethods[0] || '';
    $('#btn-send-otp').disabled = !STATE.method;
    wrap.addEventListener('change', (e) => {
      if (e.target && e.target.name === 'otp-method') {
        STATE.method = e.target.value;
        $('#btn-send-otp').disabled = !STATE.method;
      }
    });
  }

  async function handleLogin() {
    STATE.username = $('#f-username').value.trim();
    STATE.password = $('#f-password').value;
    if (!STATE.username || !STATE.password) {
      setError('Please enter both email and password.');
      return;
    }
    showStep('logging-in');
    try {
      const res = await homebridge.request('/discover', {
        username: STATE.username,
        password: STATE.password,
      });
      if (res.ok) {
        // No 2FA — save and finish.
        await saveConfig({ username: STATE.username, password: STATE.password });
        $('#success-detail').textContent = 'Signed in (no 2FA required).';
        showStep('success');
        return;
      }
      if (res.otp_required) {
        STATE.availableMethods = res.methods || [];
        STATE.hints = res.hints || {};
        renderOtpMethods();
        showStep('otp-pick');
        return;
      }
      setError(res.error || 'Login failed.');
    } catch (e) {
      setError(e?.message || String(e));
    }
  }

  async function handleSendOtp() {
    if (!STATE.method) return;
    showStep('logging-in');
    try {
      const res = await homebridge.request('/request-otp', {
        username: STATE.username,
        password: STATE.password,
        method: STATE.method,
      });
      if (!res.ok) {
        setError(res.error || 'Failed to send code.');
        return;
      }
      let detail = 'A code has been sent.';
      if (STATE.method === 'sms' && STATE.hints.sms) detail = `Code sent via SMS to ${STATE.hints.sms}.`;
      else if (STATE.method === 'email' && STATE.hints.email) detail = `Code sent to ${STATE.hints.email}.`;
      else if (STATE.method === 'app') detail = 'Open your authenticator app and enter the current code.';
      $('#otp-sent-detail').textContent = detail;
      $('#f-otp').value = '';
      showStep('otp-enter');
      $('#f-otp').focus();
    } catch (e) {
      setError(e?.message || String(e));
    }
  }

  async function handleVerify() {
    const code = $('#f-otp').value.trim();
    if (!code) {
      setError('Enter the 6-digit code first.');
      return;
    }
    showStep('saving');
    try {
      const res = await homebridge.request('/submit-otp', {
        username: STATE.username,
        password: STATE.password,
        method: STATE.method,
        code,
      });
      if (!res.ok) {
        setError(res.error || 'Verification failed.');
        return;
      }
      await saveConfig({
        username: STATE.username,
        password: STATE.password,
        mfaCookie: res.cookie,
      });
      $('#success-detail').textContent = `Signed in as ${STATE.username} and saved a trusted-device cookie.`;
      showStep('success');
    } catch (e) {
      setError(e?.message || String(e));
    }
  }

  function wireEvents() {
    $('#btn-login').addEventListener('click', handleLogin);
    $('#btn-send-otp').addEventListener('click', handleSendOtp);
    $('#btn-otp-back').addEventListener('click', () => showStep('idle'));
    $('#btn-verify').addEventListener('click', handleVerify);
    $('#btn-resend').addEventListener('click', handleSendOtp);
    $('#btn-restart-flow').addEventListener('click', () => showStep('idle'));
    $('#btn-retry').addEventListener('click', () => showStep('idle'));
    $('#f-otp').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') handleVerify();
    });
  }

  async function boot() {
    wireEvents();
    await loadExistingConfig();
    showStep('idle');
  }

  boot().catch((e) => setError(e?.message || String(e)));
})();
