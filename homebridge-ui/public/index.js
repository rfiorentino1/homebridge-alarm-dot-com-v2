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
    existing: null,
  };

  const PLATFORM_NAME = 'AlarmDotComV2';
  const REQUEST_WATCHDOG_MS = 75_000;

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
    safeToast('error', msg);
  }

  function safeToast(kind, msg) {
    try {
      const t = homebridge && homebridge.toast;
      if (!t) return;
      if (kind === 'error' && t.error) t.error(msg);
      else if (kind === 'success' && t.success) t.success(msg);
      else if (kind === 'warning' && t.warning) t.warning(msg);
      else if (t.info) t.info(msg);
    } catch (e) {
      // toast API may not exist in older homebridge-config-ui-x — ignore.
    }
  }

  // Race a homebridge.request() against a watchdog so the UI never sits on a
  // spinner indefinitely if the round-trip silently drops (we saw this in the
  // wild — /request-otp never landed at the backend with no error surfaced).
  async function requestWithWatchdog(route, payload) {
    let watchdog;
    const timeout = new Promise((_resolve, reject) => {
      watchdog = setTimeout(
        () => reject(new Error(
          `No response from backend after ${REQUEST_WATCHDOG_MS / 1000}s on ${route}. ` +
            'Check the Homebridge log for ui_auth helper errors.',
        )),
        REQUEST_WATCHDOG_MS,
      );
    });
    try {
      return await Promise.race([homebridge.request(route, payload), timeout]);
    } finally {
      clearTimeout(watchdog);
    }
  }

  async function loadExistingConfig() {
    try {
      const blocks = await homebridge.getPluginConfig();
      const current = (blocks || []).find((b) => b && b.platform === PLATFORM_NAME);
      if (current) {
        STATE.existing = current;
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

  function hasConfiguredAccount() {
    const e = STATE.existing;
    return !!(e && e.username && e.password);
  }

  function showConnectedStep() {
    const e = STATE.existing || {};
    $('#connected-username').textContent = e.username || '(unknown)';
    if (e.mfaCookie) {
      $('#connected-detail').textContent =
        'A trusted-device cookie is on file, so no 2FA prompt is needed at startup.';
    } else {
      $('#connected-detail').textContent =
        'No 2FA cookie is stored (your account either has 2FA disabled, or the daemon is using ' +
          'password-only login).';
    }
    showStep('connected');
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
    STATE.existing = updated;
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
    safeToast('info', 'Contacting Alarm.com…');
    try {
      const res = await requestWithWatchdog('/discover', {
        username: STATE.username,
        password: STATE.password,
      });
      if (!res) {
        setError('Empty response from the sign-in helper.');
        return;
      }
      if (res.ok) {
        // No 2FA — save and finish.
        await saveConfig({ username: STATE.username, password: STATE.password });
        $('#success-detail').textContent = 'Signed in (no 2FA required).';
        showStep('success');
        safeToast('success', 'Signed in to Alarm.com.');
        return;
      }
      if (res.otp_required) {
        STATE.availableMethods = res.methods || [];
        STATE.hints = res.hints || {};
        renderOtpMethods();
        showStep('otp-pick');
        safeToast('info', '2FA is enabled on this account.');
        return;
      }
      setError(res.error || 'Login failed.');
    } catch (e) {
      setError(e?.message || String(e));
    }
  }

  async function handleSendOtp() {
    if (!STATE.method) {
      setError('No 2FA method selected — pick one and try again.');
      return;
    }
    showStep('sending-otp');
    const method = STATE.method;
    let detailWhilePending = 'Asking Alarm.com to send the verification code.';
    if (method === 'sms') detailWhilePending = 'Asking Alarm.com to text the code…';
    else if (method === 'email') detailWhilePending = 'Asking Alarm.com to email the code…';
    else if (method === 'app') detailWhilePending = 'Skipping send (authenticator app generates its own).';
    $('#sending-otp-detail').textContent = detailWhilePending;
    safeToast('info', detailWhilePending);
    try {
      const res = await requestWithWatchdog('/request-otp', {
        username: STATE.username,
        password: STATE.password,
        method,
      });
      if (!res) {
        setError('Empty response from the send-code helper.');
        return;
      }
      if (!res.ok) {
        setError(res.error || 'Failed to send code.');
        return;
      }
      let detail = 'A code has been sent.';
      if (method === 'sms' && STATE.hints.sms) detail = `Code sent via SMS to ${STATE.hints.sms}.`;
      else if (method === 'email' && STATE.hints.email) detail = `Code sent to ${STATE.hints.email}.`;
      else if (method === 'app') detail = 'Open your authenticator app and enter the current code.';
      $('#otp-sent-detail').textContent = detail;
      $('#f-otp').value = '';
      showStep('otp-enter');
      safeToast('success', detail);
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
    safeToast('info', 'Verifying code with Alarm.com…');
    try {
      const res = await requestWithWatchdog('/submit-otp', {
        username: STATE.username,
        password: STATE.password,
        method: STATE.method,
        code,
      });
      if (!res) {
        setError('Empty response from the verify helper.');
        return;
      }
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
      safeToast('success', 'Signed in and trusted-device cookie saved.');
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
    $('#btn-resign').addEventListener('click', () => showStep('idle'));
    $('#f-otp').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') handleVerify();
    });
  }

  async function boot() {
    wireEvents();
    // Show the schema-driven form (Expose Security Panel, Contact Sensors,
    // Motion Sensors, bypass, advanced options, etc.) underneath the custom
    // UI. homebridge-config-ui-x hides it by default when customUi:true is
    // set, and we want both visible — the custom UI is just a wizard for the
    // credential/2FA flow.
    try {
      if (homebridge.showSchemaForm) homebridge.showSchemaForm();
    } catch (e) {
      // older homebridge-config-ui-x: schema form is shown by default.
    }
    await loadExistingConfig();
    if (hasConfiguredAccount()) {
      showConnectedStep();
    } else {
      showStep('idle');
    }
  }

  boot().catch((e) => setError(e?.message || String(e)));
})();
