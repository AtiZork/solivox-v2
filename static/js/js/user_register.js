document.addEventListener('DOMContentLoaded', () => {
  const registerBtn = document.getElementById('registerBtn');
  const loginBtn = document.getElementById('loginBtn');
  const logoutBtn = document.getElementById('logoutBtn');
  const userInfo = document.getElementById('userInfo');
  const welcomeUser = document.getElementById('welcomeUser');

  // Show modals on button click
  registerBtn.addEventListener('click', () => new bootstrap.Modal(document.getElementById('registerModal')).show());
  loginBtn.addEventListener('click', () => new bootstrap.Modal(document.getElementById('loginModal')).show());

  // Register form submission
  document.getElementById('registerForm').onsubmit = async (e) => {
    e.preventDefault();
    const form = e.target;
    const data = {
      username: form.username.value.trim(),
      email: form.email.value.trim(),
      password: form.password.value
    };
    const res = await fetch('/register', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    const result = await res.json();
    alert(result.msg || 'Registration completed');
    if (res.ok) bootstrap.Modal.getInstance(document.getElementById('registerModal')).hide();
  };

  // Login form submission
  document.getElementById('loginForm').onsubmit = async (e) => {
    e.preventDefault();
    const form = e.target;
    const data = {
      username: form.username.value.trim(),
      password: form.password.value
    };
    const res = await fetch('/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    const result = await res.json();
    if (result.access_token) {
      localStorage.setItem('token', result.access_token);
      bootstrap.Modal.getInstance(document.getElementById('loginModal')).hide();
       // Refresh the page to reload with authenticated state
      location.reload();
      loadUserInfo();
    } else {
      alert(result.msg || 'Login failed');
    }
  };

  // Load user info on page load or after login
  async function loadUserInfo() {
    const token = localStorage.getItem('token');
    if (!token) {
      userInfo.style.display = 'none';
      registerBtn.style.display = 'inline-block';
      loginBtn.style.display = 'inline-block';
      logoutBtn.style.display = 'none'; // <--- Add this
      return;
    }
    const res = await fetch('/user', {
      headers: { 'Authorization': `Bearer ${token}` }
    });
    if (res.ok) {
      const user = await res.json();
      welcomeUser.textContent = `Welcome, ${user.username}`;
      userInfo.style.display = 'flex';
      registerBtn.style.display = 'none';
      loginBtn.style.display = 'none';
      logoutBtn.style.display = 'inline-block'; // ✅ Add this line
    } else {
      localStorage.removeItem('token');
      userInfo.style.display = 'none';
      registerBtn.style.display = 'inline-block';
      loginBtn.style.display = 'inline-block';
      logoutBtn.style.display = 'none';
    }
  }

  // Logout
  logoutBtn.addEventListener('click', () => {
    localStorage.removeItem('token');
    const walletGroup = document.getElementById('openDrawer');
    walletGroup.style.display = 'none';

    loadUserInfo();
    location.reload(); // Reload the page after logout
  });

  // Initial check on page load
  loadUserInfo();
});
