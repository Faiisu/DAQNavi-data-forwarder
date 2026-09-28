import { $ } from './ui.js';

export function initPasswordModal({api, setMessage}) {
const operatorProfile = $('operator-profile');
const pwdModal = $('password-modal');
const btnOpenPwd = $('btn-open-change-password');
const btnClosePwd = $('btn-close-pwd-modal');
const btnCancelPwd = $('btn-cancel-pwd');
const formChangePwd = $('change-password-form');
const pwdAlert = $('pwd-modal-alert');
const btnSubmitPwd = $('btn-submit-pwd');

function showPwdAlert(msg, type) {
  if (!pwdAlert) return;
  pwdAlert.className = `inline-result ${type}`;
  pwdAlert.textContent = msg;
  pwdAlert.classList.remove('hidden');
}

function openPasswordModal() {
  if (!pwdModal) return;
  if (operatorProfile) operatorProfile.open = false;
  pwdModal.classList.remove('hidden');
  if (pwdAlert) {
    pwdAlert.className = 'inline-result hidden';
    pwdAlert.textContent = '';
  }
  if (formChangePwd) formChangePwd.reset();
  setTimeout(() => {
    const cur = $('pwd-current');
    if (cur) cur.focus();
  }, 50);
}

function closePasswordModal() {
  if (!pwdModal) return;
  pwdModal.classList.add('hidden');
  if (formChangePwd) formChangePwd.reset();
}

if (btnOpenPwd) btnOpenPwd.addEventListener('click', openPasswordModal);
if (btnClosePwd) btnClosePwd.addEventListener('click', closePasswordModal);
if (btnCancelPwd) btnCancelPwd.addEventListener('click', closePasswordModal);

if (operatorProfile) {
  document.addEventListener('click', (e) => {
    if (!operatorProfile.contains(e.target)) operatorProfile.open = false;
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && operatorProfile.open) {
      operatorProfile.open = false;
      operatorProfile.querySelector('summary').focus();
    }
  });
}

if (pwdModal) {
  pwdModal.addEventListener('click', (e) => {
    if (e.target === pwdModal) closePasswordModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !pwdModal.classList.contains('hidden')) {
      closePasswordModal();
    }
  });
}

if (formChangePwd) {
  formChangePwd.addEventListener('submit', async (e) => {
    e.preventDefault();
    const current_password = $('pwd-current').value;
    const new_password = $('pwd-new').value;
    const confirm_password = $('pwd-confirm').value;

    if (!current_password) {
      showPwdAlert('Please enter your current password.', 'error');
      return;
    }
    if (!new_password || new_password.length < 8) {
      showPwdAlert('New password must be at least 8 characters.', 'error');
      return;
    }
    if (new_password !== confirm_password) {
      showPwdAlert('New password and confirmation do not match.', 'error');
      return;
    }
    if (new_password === current_password) {
      showPwdAlert('New password must be different from current password.', 'error');
      return;
    }

    btnSubmitPwd.disabled = true;
    btnSubmitPwd.textContent = 'Updating...';
    try {
      const response = await api('/api/auth/change-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ current_password, new_password, confirm_password })
      });
      const data = await response.json();
      if (!response.ok || data.status !== 'ok') {
        throw new Error(data.message || data.error || 'Failed to update password');
      }
      showPwdAlert('Password updated successfully!', 'success');
      setTimeout(() => {
        closePasswordModal();
        setMessage('Operator password changed successfully.', 'success');
      }, 1200);
    } catch (err) {
      showPwdAlert(err.message, 'error');
    } finally {
      btnSubmitPwd.disabled = false;
      btnSubmitPwd.textContent = 'Update Password';
    }
  });
}

}
